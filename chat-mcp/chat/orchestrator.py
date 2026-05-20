"""Tool-calling loop: LLM -> tool calls -> tool results -> LLM ... -> stop.

Yields SSE-event dicts (caller serialises). Bounded at MAX_ITERS to prevent
runaway tool-call cycles.
"""

import asyncio
import json
import logging
from typing import AsyncIterator

from .llm import LLMClient
from .mcp_pool import MCPPool

logger = logging.getLogger(__name__)

MAX_ITERS = 10

import os
from pathlib import Path


def _load_system_prompt() -> str:
    """Load system prompt with this precedence:
    1) $CHAT_MCP_SYSTEM_PROMPT_FILE
    2) ~/.chat-mcp/system_prompt.md
    3) ./default_system_prompt.md (bundled fallback)
    """
    candidates = []
    env = os.environ.get("CHAT_MCP_SYSTEM_PROMPT_FILE")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.home() / ".chat-mcp" / "system_prompt.md")
    candidates.append(Path(__file__).parent / "default_system_prompt.md")
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text()
        except OSError:
            continue
    return "You are a helpful assistant."


SYSTEM_PROMPT = _load_system_prompt()


class Orchestrator:
    def __init__(self, llm: LLMClient, pool: MCPPool) -> None:
        self._llm = llm
        self._pool = pool

    def build_messages(self, history: list[dict], user_text: str) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        msgs.extend(history)
        msgs.append({"role": "user", "content": user_text})
        return msgs

    async def run(self, messages: list[dict]) -> AsyncIterator[dict]:
        tools = self._pool.all_tools_as_openai_schema()

        for iteration in range(MAX_ITERS):
            assistant_content = ""
            assistant_reasoning = ""
            tool_calls: list[dict] | None = None
            finish_reason: str | None = None

            try:
                async for chunk in self._llm.chat(messages, tools=tools):
                    if chunk.delta_content:
                        assistant_content += chunk.delta_content
                        yield {"type": "content", "text": chunk.delta_content}
                    if chunk.delta_reasoning:
                        assistant_reasoning += chunk.delta_reasoning
                        yield {"type": "thinking", "text": chunk.delta_reasoning}
                    if chunk.finish_reason is not None:
                        finish_reason = chunk.finish_reason
                    if chunk.tool_calls is not None:
                        tool_calls = chunk.tool_calls
            except Exception as exc:
                logger.exception("LLM stream error")
                yield {"type": "error", "message": f"LLM error: {exc}"}
                return

            # Lumen quirk: content may be empty while reasoning has the answer
            if not assistant_content and assistant_reasoning and not tool_calls:
                assistant_content = assistant_reasoning

            assistant_msg: dict = {"role": "assistant", "content": assistant_content or None}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls or finish_reason == "stop":
                yield {"type": "done"}
                return

            # Announce + execute tool calls in parallel
            async def _run_call(tc: dict) -> tuple[dict, str]:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = {}
                    return tc, f"[error] could not parse arguments JSON: {raw_args}"
                try:
                    result = await self._pool.call(name, args)
                except Exception as exc:
                    result = f"[error] {exc}"
                return tc, result

            announcements: list[dict] = []
            for tc in tool_calls:
                fn = tc.get("function") or {}
                try:
                    parsed_args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    parsed_args = {"_raw": fn.get("arguments", "")}
                announcements.append({
                    "type": "tool_call",
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "args": parsed_args,
                })
            for a in announcements:
                yield a

            results = await asyncio.gather(*(_run_call(tc) for tc in tool_calls))

            for tc, result in results:
                preview = result if len(result) <= 1200 else result[:1200] + "\n…[truncated]"
                yield {
                    "type": "tool_result",
                    "id": tc.get("id"),
                    "name": (tc.get("function") or {}).get("name"),
                    "preview": preview,
                }
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": result,
                })

        yield {"type": "error", "message": f"Bounded at {MAX_ITERS} tool-call iterations."}
        yield {"type": "done"}
