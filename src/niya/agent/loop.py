import asyncio
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from niya.agent.context import Conversation
from niya.clock import Clock
from niya.inbox import InboundItem
from niya.llm import ChatResult
from niya.tools.registry import ToolContext, ToolRegistry

ITERATION_LIMIT_NOTE = "已達到這一回合的工具呼叫上限，請直接向主人總結目前的進度。"


class ChatModel(Protocol):
    async def chat(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        task: str = "chat",
        max_tokens: int | None = None,
    ) -> ChatResult: ...


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    text: str
    iterations: int
    prompt_tokens: int

    @property
    def silent(self) -> bool:
        return self.text.lstrip().startswith("[SILENT]")


def new_turn_id() -> str:
    return f"t-{secrets.token_hex(6)}"


class Agent:
    def __init__(
        self,
        *,
        llm: ChatModel,
        model: str,
        tools: ToolRegistry,
        conversation: Conversation,
        clock: Clock,
        max_iterations: int = 40,
        on_turn_end: Callable[[str], None] | None = None,
    ) -> None:
        if not model:
            raise ValueError("請先設定模型（niya.toml 的 agent.model 或環境變數 NIYA_MODEL）。")
        self.llm = llm
        self.model = model
        self.tools = tools
        self.conversation = conversation
        self.clock = clock
        self.max_iterations = max_iterations
        self.on_turn_end = on_turn_end
        self.last_prompt_tokens = 0

    async def run_turn(self, items: Sequence[InboundItem]) -> TurnResult:
        if not items:
            raise ValueError("一回合至少要有一則訊息。")
        conv = self.conversation
        turn_id = new_turn_id()
        conv.start_turn(turn_id, source=items[0].speaker, ts=self.clock.now())
        conv.add_user_items(turn_id, items)
        schemas = self.tools.schemas() or None
        ctx = ToolContext(session_id=conv.session_id, turn_id=turn_id, now=self.clock.now)
        try:
            for iteration in range(1, self.max_iterations + 1):
                result = await self._chat(schemas)
                conv.add_assistant(turn_id, result, ts=self.clock.now())
                if not result.tool_calls:
                    return self._finish(turn_id, iteration)
                outcomes = await self.tools.execute(result.tool_calls, ctx)
                for outcome in outcomes:
                    conv.add_tool_result(turn_id, outcome, ts=self.clock.now())
            conv.add_system_line(turn_id, ITERATION_LIMIT_NOTE, ts=self.clock.now())
            result = await self._chat(schemas, tool_choice="none" if schemas else None)
            conv.add_assistant(turn_id, result, ts=self.clock.now())
            # Some providers ignore tool_choice="none"; never leave calls without results in history.
            conv.close_dangling_tool_calls(turn_id, "錯誤：已達工具呼叫上限，這個呼叫沒有執行。", ts=self.clock.now())
            return self._finish(turn_id, self.max_iterations + 1)
        except asyncio.CancelledError:
            conv.close_dangling_tool_calls(turn_id, "[已中斷]", ts=self.clock.now())
            conv.finish_turn(turn_id, ts=self.clock.now())
            raise
        finally:
            if self.on_turn_end:
                self.on_turn_end(conv.session_id)

    async def _chat(self, schemas: list[dict[str, Any]] | None, tool_choice: str | None = None) -> ChatResult:
        result = await self.llm.chat(
            model=self.model,
            messages=self.conversation.api_messages(),
            tools=schemas,
            tool_choice=tool_choice,
            task="chat",
        )
        self.last_prompt_tokens = result.usage.prompt_tokens
        return result

    def _finish(self, turn_id: str, iterations: int) -> TurnResult:
        self.conversation.finish_turn(turn_id, ts=self.clock.now())
        last = next((m for m in reversed(self.conversation.messages) if m.role == "assistant"), None)
        return TurnResult(turn_id=turn_id, text=(last.content or "") if last else "", iterations=iterations, prompt_tokens=self.last_prompt_tokens)
