"""Async client for Lumen's OpenAI-compatible /v1/chat/completions endpoint.

Streams SSE. Yields ChatChunk objects with separate `delta_content`,
`delta_reasoning`, and accumulated `tool_calls`. Handles 429 with backoff.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

from .config import LumenConfig

logger = logging.getLogger(__name__)


@dataclass
class ChatChunk:
    delta_content: str = ""
    delta_reasoning: str = ""
    tool_calls: list[dict] | None = None  # set only when finalised at finish
    finish_reason: str | None = None


@dataclass
class _PartialToolCall:
    id: str = ""
    name: str = ""
    arguments: str = ""
    index: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


class LLMClient:
    def __init__(self, cfg: LumenConfig) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_retries: int = 4,
    ) -> AsyncIterator[ChatChunk]:
        body: dict = {
            "model": self._cfg.model,
            "messages": messages,
            "stream": True,
            "max_tokens": self._cfg.max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        # Ollama-specific: keep model in VRAM for 2h to avoid cold-load delays
        body["keep_alive"] = "2h"

        url = self._cfg.endpoint.rstrip("/") + "/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        attempt = 0
        while True:
            try:
                async for chunk in self._stream_once(url, headers, body):
                    yield chunk
                return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status == 429 and attempt < max_retries:
                    delay = min(2 ** attempt * 4, 30)
                    logger.warning("Lumen 429 — backing off %ds (attempt %d)", delay, attempt + 1)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise

    async def _stream_once(
        self, url: str, headers: dict, body: dict,
    ) -> AsyncIterator[ChatChunk]:
        # Accumulate tool calls across chunks (OpenAI streams them in pieces by index)
        partials: dict[int, _PartialToolCall] = {}
        last_finish: str | None = None

        async with self._client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                txt = await resp.aread()
                resp_text = txt.decode("utf-8", errors="replace")[:2000]
                logger.error("Lumen HTTP %d: %s", resp.status_code, resp_text)
                resp.raise_for_status()

            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith(":"):
                    continue  # SSE comment / keepalive
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    logger.warning("Bad SSE payload: %s", payload[:200])
                    continue

                choices = obj.get("choices") or []
                if not choices:
                    continue
                ch = choices[0]
                delta = ch.get("delta") or {}

                content_piece = delta.get("content") or ""
                reasoning_piece = delta.get("reasoning_content") or ""

                tcs = delta.get("tool_calls")
                if tcs:
                    for tc in tcs:
                        idx = tc.get("index", 0)
                        p = partials.setdefault(idx, _PartialToolCall(index=idx))
                        if tc.get("id"):
                            p.id = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            p.name = (p.name or "") + fn["name"] if not p.name else fn["name"]
                            # OpenAI sends the name in one chunk usually; overwrite is safer.
                            p.name = fn["name"]
                        if fn.get("arguments"):
                            p.arguments += fn["arguments"]

                finish = ch.get("finish_reason")
                if finish:
                    last_finish = finish

                if content_piece or reasoning_piece:
                    yield ChatChunk(
                        delta_content=content_piece,
                        delta_reasoning=reasoning_piece,
                    )

        final_tcs = None
        if partials:
            final_tcs = [p.to_dict() for p in sorted(partials.values(), key=lambda x: x.index)]

        yield ChatChunk(
            tool_calls=final_tcs,
            finish_reason=last_finish or ("tool_calls" if final_tcs else "stop"),
        )
