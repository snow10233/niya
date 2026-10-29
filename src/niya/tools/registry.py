import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from niya.llm.openrouter import ToolCall

log = logging.getLogger(__name__)


class ToolError(Exception):
    """Expected tool failure; the message is shown to the model as-is."""


@dataclass
class ToolContext:
    session_id: str
    turn_id: str
    now: Callable[[], float]
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolText:
    """A handler result that came from outside niya (web page, curl output, MCP server)."""

    text: str
    external: bool = False
    source: str | None = None


Handler = Callable[[dict[str, Any], ToolContext], Awaitable["str | ToolText"]]
# Applied to every tool result before truncation (secret redaction, sanitizing); the bool says it is external.
OutputFilter = Callable[[str, "Tool", bool], str]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    toolset: str = "core"
    parallel_safe: bool = False
    timeout_s: float = 120.0
    max_result_chars: int = 20_000
    # "file": output is file content (secrets get a non-reusable sentinel); "external": always from outside niya.
    output_kind: str = "default"

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass(frozen=True)
class ToolOutcome:
    call: ToolCall
    content: str
    full_content: str
    is_error: bool = False
    external_source: str | None = None


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…（輸出過長，中間省略 {omitted} 字元）…\n{text[-tail:]}"


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool] = (),
        *,
        output_filters: Sequence[OutputFilter] = (),
        on_outcome: Callable[[ToolContext, "ToolOutcome"], None] | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._schemas: list[dict[str, Any]] | None = None
        self.output_filters = list(output_filters)
        # Called as soon as each result exists, so e.g. taint from a web read applies to the very next call.
        self.on_outcome = on_outcome
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具名稱重複：{tool.name}")
        self._tools[tool.name] = tool
        self._schemas = None

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def subset(self, names: Iterable[str]) -> "ToolRegistry":
        return ToolRegistry((self._tools[n] for n in names if n in self._tools), output_filters=self.output_filters, on_outcome=self.on_outcome)

    def schemas(self) -> list[dict[str, Any]]:
        # Sorted and cached so the tools block stays byte-identical for prefix caching.
        if self._schemas is None:
            self._schemas = [self._tools[name].schema() for name in sorted(self._tools)]
        return self._schemas

    async def execute(self, calls: Sequence[ToolCall], ctx: ToolContext) -> list[ToolOutcome]:
        results: list[ToolOutcome] = []
        i = 0
        while i < len(calls):
            if self._is_parallel_safe(calls[i]):
                j = i
                while j < len(calls) and self._is_parallel_safe(calls[j]):
                    j += 1
                batch = await asyncio.gather(*(self._run(call, ctx) for call in calls[i:j]))
                i = j
            else:
                batch = [await self._run(calls[i], ctx)]
                i += 1
            if self.on_outcome:
                for outcome in batch:
                    self.on_outcome(ctx, outcome)
            results.extend(batch)
        return results

    def _is_parallel_safe(self, call: ToolCall) -> bool:
        tool = self._tools.get(call.name)
        return bool(tool and tool.parallel_safe)

    async def _run(self, call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        tool = self._tools.get(call.name)
        if tool is None:
            return _error(call, f"沒有這個工具：{call.name}")
        try:
            args = json.loads(call.arguments) if call.arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return _error(call, f"參數不是合法的 JSON：{exc.msg}（第 {exc.pos} 個字元）")
        if not isinstance(args, dict):
            return _error(call, "參數必須是 JSON 物件。")
        try:
            async with asyncio.timeout(tool.timeout_s):
                result = await tool.handler(args, ctx)
        except TimeoutError:
            return _error(call, f"工具執行逾時（{tool.timeout_s:g} 秒）。")
        except ToolError as exc:
            return _error(call, str(exc))
        except Exception as exc:
            log.exception("tool %s failed", call.name)
            return _error(call, f"工具執行失敗：{type(exc).__name__}: {exc}")
        if isinstance(result, ToolText):
            text, external, source = result.text, result.external or tool.output_kind == "external", result.source
        else:
            text, external, source = result, tool.output_kind == "external", None
        for output_filter in self.output_filters:
            text = output_filter(text, tool, external)
        return ToolOutcome(
            call=call,
            content=truncate(text, tool.max_result_chars),
            full_content=text,
            external_source=(source or tool.name) if external else None,
        )


def _error(call: ToolCall, message: str) -> ToolOutcome:
    text = f"錯誤：{message}"
    return ToolOutcome(call=call, content=text, full_content=text, is_error=True)
