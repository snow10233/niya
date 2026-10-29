import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from niya.llm.openrouter import ChatResult, ToolCall, Usage

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
        reasoning: dict[str, Any] | None = None,
    ) -> ChatResult:
        self.requests.append({"model": model, "messages": [dict(m) for m in messages], "tools": tools, "tool_choice": tool_choice, "task": task, "reasoning": reasoning})
        if self.block:
            await self.block.wait()
        if not self.results:
            raise AssertionError("FakeLLM ran out of scripted results")
        return self.results.pop(0)


class HashEmbedder:
    """Deterministic stand-in for an embedding model: texts sharing character bigrams point the same way."""

    model = "hash@256"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.calls = 0
        self.fail = False

    async def embed(self, texts):
        import zlib

        self.calls += 1
        if self.fail:
            raise RuntimeError("embedding service down")
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            chars = [c for c in text.lower() if not c.isspace()]
            for a, b in zip(chars, chars[1:]):
                vec[zlib.crc32((a + b).encode()) % self.dim] += 1.0
            out.append(vec)
        return out
