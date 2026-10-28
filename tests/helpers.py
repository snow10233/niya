import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from niya.llm import ChatResult, ToolCall, Usage

TZ = ZoneInfo("Asia/Taipei")


def ts(text: str) -> float:
    """'2026-10-28 21:30' in Asia/Taipei → epoch seconds."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()


def reply(content: str | None = None, *calls: tuple[str, str, str], prompt_tokens: int = 100) -> ChatResult:
    return ChatResult(
        content=content,
        tool_calls=tuple(ToolCall(id=i, name=n, arguments=a) for i, n, a in calls),
        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=10),
        model="test/model",
    )


class FakeLLM:
    """Returns scripted results in order and records every request."""

    def __init__(self, *results: ChatResult) -> None:
        self.results = list(results)
        self.requests: list[dict[str, Any]] = []
        self.block: asyncio.Event | None = None

    async def chat(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        task: str = "chat",
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.requests.append({"model": model, "messages": [dict(m) for m in messages], "tools": tools, "tool_choice": tool_choice, "task": task})
        if self.block:
            await self.block.wait()
        if not self.results:
            raise AssertionError("FakeLLM ran out of scripted results")
        return self.results.pop(0)
