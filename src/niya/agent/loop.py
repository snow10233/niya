import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from niya.agent.budget import CONTEXT_FULL_NOTE, HARD, estimate_all, over
from niya.agent.context import Conversation
from niya.core.clock import Clock
from niya.core.inbox import InboundItem, turn_source
from niya.llm.openrouter import ChatResult
from niya.tools.registry import ToolContext, ToolRegistry

log = logging.getLogger(__name__)

ITERATION_LIMIT_NOTE = "已達到這一回合的工具呼叫上限，請直接向主人總結目前的進度。"
# (conversation, turn_id, items) -> a <recalled_memories> block, or None when nothing comes to mind.
RecallHook = Callable[[Conversation, str, Sequence[InboundItem]], Awaitable[str | None]]


async def recall_into(hook: RecallHook | None, conv: Conversation, turn_id: str, items: Sequence[InboundItem]) -> None:
    """Memories that come to mind are put on top of the turn; recall trouble never costs the turn."""
    if hook is None:
        return
    try:
        block = await hook(conv, turn_id, items)
    except Exception:
        log.exception("recall failed")
        return
    if block:
        conv.set_recall_block(turn_id, block)


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
        reasoning: dict[str, Any] | None = None,
    ) -> ChatResult: ...


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    text: str
    iterations: int
    prompt_tokens: int
    context_full: bool = False

    @property
    def silent(self) -> bool:
        return self.text.lstrip().startswith("[SILENT]")


def new_turn_id() -> str:
    return f"t-{secrets.token_hex(6)}"


class Agent:
    """niya's own tool loop over an OpenAI-compatible chat API (OpenRouter)."""

    engine = "openrouter"

    def __init__(
        self,
        *,
        llm: ChatModel,
        model: str,
        tools: ToolRegistry,
        conversation: Conversation,
        clock: Clock,
        max_iterations: int = 40,
        max_context_tokens: int = 200_000,
        effort: str | None = None,
        on_turn_end: Callable[[str], None] | None = None,
        recall: RecallHook | None = None,
    ) -> None:
        if not model:
            raise ValueError("請先設定模型（niya.toml 的 agent.model 或環境變數 NIYA_MODEL）。")
        self.llm = llm
        self.model = model
        self.tools = tools
        self.clock = clock
        self.max_iterations = max_iterations
        self.effort = effort or None
        self.on_turn_end = on_turn_end
        self.recall = recall
        self.max_context_tokens = max_context_tokens
        self.context_limit = max_context_tokens
        self._limit_for: str | None = None
        self.conversation = conversation

    @property
    def conversation(self) -> Conversation:
        return self._conversation

    @conversation.setter
    def conversation(self, conversation: Conversation) -> None:
        # A new session starts from an empty context.
        self._conversation = conversation
        self.last_prompt_tokens = 0
        self._counted = len(conversation.messages)

    async def run_turn(self, items: Sequence[InboundItem]) -> TurnResult:
        if not items:
            raise ValueError("一回合至少要有一則訊息。")
        conv = self.conversation
        turn_id = new_turn_id()
        conv.start_turn(turn_id, source=turn_source(items), ts=self.clock.now())
        conv.add_user_items(turn_id, items)
        if conv.engine is None:
            conv.bind_engine(self.engine)
        await recall_into(self.recall, conv, turn_id, items)
        schemas = self.tools.schemas() or None
        ctx = ToolContext(session_id=conv.session_id, turn_id=turn_id, now=self.clock.now)
        await self._refresh_limit()
        try:
            for iteration in range(1, self.max_iterations + 1):
                if iteration > 1 and over(self.context_tokens(), self.context_limit, HARD):
                    return await self._wrap_up(turn_id, schemas, CONTEXT_FULL_NOTE, iteration, context_full=True)
                result = await self._chat(schemas)
                conv.add_assistant(turn_id, result, ts=self.clock.now())
                if not result.tool_calls:
                    return self._finish(turn_id, iteration)
                outcomes = await self.tools.execute(result.tool_calls, ctx)
                for outcome in outcomes:
                    conv.add_tool_result(turn_id, outcome, ts=self.clock.now())
            return await self._wrap_up(turn_id, schemas, ITERATION_LIMIT_NOTE, self.max_iterations + 1)
        except asyncio.CancelledError:
            conv.close_dangling_tool_calls(turn_id, "[已中斷]", ts=self.clock.now())
            conv.finish_turn(turn_id, ts=self.clock.now())
            raise
        finally:
            if self.on_turn_end:
                self.on_turn_end(conv.session_id)

    def context_tokens(self) -> int:
        """The last reported prompt size plus a generous estimate of everything added since."""
        added = self.conversation.messages[self._counted :]
        return self.last_prompt_tokens + estimate_all(m.api_content if m.api_content is not None else m.content for m in added)

    async def set_model(self, model: str) -> None:
        self.model = model

    async def set_effort(self, effort: str | None) -> None:
        self.effort = effort or None

    async def aclose(self) -> None:
        close = getattr(self.llm, "aclose", None)
        if close is not None:
            await close()

    async def _chat(self, schemas: list[dict[str, Any]] | None, tool_choice: str | None = None) -> ChatResult:
        result = await self.llm.chat(
            model=self.model,
            messages=self.conversation.api_messages(),
            tools=schemas,
            tool_choice=tool_choice,
            task="chat",
            reasoning={"effort": self.effort} if self.effort else None,
        )
        self.last_prompt_tokens = result.usage.prompt_tokens
        self._counted = len(self.conversation.messages)
        return result

    async def _refresh_limit(self) -> None:
        if self._limit_for == self.model:
            return
        window = None
        lookup = getattr(self.llm, "context_length", None)
        if lookup is not None:
            try:
                window = await lookup(self.model)
            except Exception:  # an unknown window just means the configured cap applies
                window = None
        self.context_limit = min(window or self.max_context_tokens, self.max_context_tokens)
        self._limit_for = self.model

    async def _wrap_up(self, turn_id: str, schemas: list[dict[str, Any]] | None, note: str, iterations: int, *, context_full: bool = False) -> TurnResult:
        conv = self.conversation
        conv.add_system_line(turn_id, note, ts=self.clock.now())
        result = await self._chat(schemas, tool_choice="none" if schemas else None)
        conv.add_assistant(turn_id, result, ts=self.clock.now())
        # Some providers ignore tool_choice="none"; never leave calls without results in history.
        conv.close_dangling_tool_calls(turn_id, "錯誤：這個呼叫沒有執行。", ts=self.clock.now())
        return self._finish(turn_id, iterations, context_full=context_full)

    def _finish(self, turn_id: str, iterations: int, *, context_full: bool = False) -> TurnResult:
        self.conversation.finish_turn(turn_id, ts=self.clock.now())
        last = next((m for m in reversed(self.conversation.messages) if m.role == "assistant"), None)
        text = (last.content or "") if last else ""
        return TurnResult(turn_id=turn_id, text=text, iterations=iterations, prompt_tokens=self.last_prompt_tokens, context_full=context_full)
