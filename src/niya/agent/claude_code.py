"""Run niya's turns on the official Claude Code engine (claude-agent-sdk), e.g. with a Claude subscription.

Claude Code owns the tool loop and the API context here. niya supplies the system prompt and its own tools
as an in-process MCP server (every call still goes through the ToolRegistry: hardline, egress gate,
redaction, taint), and mirrors each message into the database exactly like the OpenRouter loop does, so
the episodic record does not depend on which engine produced it.
"""

import asyncio
import json
import logging
import secrets
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SdkMcpTool,
    SystemMessage,
    TextBlock,
    ToolAnnotations,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)

from niya.agent.budget import CONTEXT_FULL_TOOL_TEXT, HARD, over
from niya.agent.context import Conversation
from niya.agent.loop import RecallHook, TurnResult, new_turn_id, recall_into
from niya.agent.render import render_history
from niya.core.clock import Clock
from niya.core.inbox import InboundItem, turn_source
from niya.llm.choice import effort_for
from niya.llm.claude_cli import claude_env
from niya.llm.openrouter import CallRecord, ChatResult, LLMError, Recorder, ToolCall, Usage
from niya.tools.registry import Tool, ToolContext, ToolOutcome, ToolRegistry

log = logging.getLogger(__name__)

ENGINE = "claude"
SERVER = "niya"
TOOL_PREFIX = f"mcp__{SERVER}__"
CAP_TOOL_TEXT = "錯誤：這一回合的工具呼叫已達上限，這個呼叫沒有執行。請直接向主人總結目前的進度。"
MISSING_SESSION = "No conversation found"
LOST_CONTEXT_NOTE = "這個 session 先前的對話接續不上了（Claude Code 那邊找不到紀錄），之前的內容不在 context 裡；需要的話向主人確認。"
LIMIT_LABELS = {
    "five_hour": "5 小時額度",
    "seven_day": "每週額度",
    "seven_day_opus": "每週 Opus 額度",
    "seven_day_sonnet": "每週 Sonnet 額度",
    "overage": "超額用量",
}
ERROR_TEXT = {
    "authentication_failed": "Claude 登入失效了，請執行 niya login 重新登入。",
    "billing_error": "Claude 帳號目前沒有可用的額度或訂閱。",
}


class SDKClient(Protocol):
    async def connect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_response(self) -> AsyncIterator[Any]: ...
    async def set_model(self, model: str | None = None) -> None: ...
    async def disconnect(self) -> None: ...


@dataclass
class _Pending:
    message_id: str | None
    model: str
    texts: list[str] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)


@dataclass
class _Turn:
    turn_id: str
    ctx: ToolContext
    pending: _Pending | None = None
    calls: dict[str, tuple[ToolCall, str]] = field(default_factory=dict)
    iterations: int = 0
    # Set once tools must stop: the text every further tool call gets instead of running.
    cap_text: str | None = None
    context_full: bool = False
    text: str = ""
    error: str | None = None
    rate_limit: RateLimitInfo | None = None


def _args_key(args: Any) -> str:
    return json.dumps(args, ensure_ascii=False, sort_keys=True)


def _block_text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content if part.get("type") == "text")


def _prompt_tokens(usage: Mapping[str, Any]) -> int:
    return sum(int(usage.get(key) or 0) for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


class ClaudeCodeAgent:
    engine = ENGINE

    def __init__(
        self,
        *,
        model: str,
        tools: ToolRegistry,
        conversation: Conversation,
        clock: Clock,
        config_dir: Path,
        cwd: Path,
        max_iterations: int = 40,
        max_context_tokens: int = 200_000,
        effort: str | None = None,
        secrets: Mapping[str, str] | None = None,
        on_turn_end: Callable[[str], None] | None = None,
        on_rate_limit: Callable[[RateLimitInfo], None] | None = None,
        recall: RecallHook | None = None,
        recorder: Recorder | None = None,
        client_factory: Callable[[ClaudeAgentOptions], SDKClient] = ClaudeSDKClient,
    ) -> None:
        if not model:
            raise ValueError("請先選擇模型（niya model）。")
        self.model = model
        self.tools = tools
        self.clock = clock
        self.config_dir = config_dir.resolve()
        self.cwd = cwd.resolve()
        self.max_iterations = max_iterations
        self.effort = effort_for(ENGINE, effort or "")
        self.secrets = dict(secrets or {})
        self.on_turn_end = on_turn_end
        self.on_rate_limit = on_rate_limit
        self.recall = recall
        self.recorder = recorder
        self.client_factory = client_factory
        # Claude models have windows far beyond the configured cap, so the cap is the limit.
        self.context_limit = max_context_tokens
        self.conversation = conversation
        self.sdk_tools = [self._sdk_tool(tools.get(name)) for name in tools.names()]
        self._client: SDKClient | None = None
        self._client_session: str | None = None
        self._turn: _Turn | None = None
        self._stash: dict[tuple[str, str], deque[ToolOutcome]] = defaultdict(deque)

    @property
    def conversation(self) -> Conversation:
        return self._conversation

    @conversation.setter
    def conversation(self, conversation: Conversation) -> None:
        self._conversation = conversation
        self.last_prompt_tokens = 0

    def options(self, resume: str | None = None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=self.conversation.system_prompt,
            tools=[],
            mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=self.sdk_tools)},
            strict_mcp_config=True,
            allowed_tools=[TOOL_PREFIX + t.name for t in self.sdk_tools],
            # Anything not listed above is refused instead of prompting; niya's own guards run inside the tools.
            permission_mode="dontAsk",
            setting_sources=[],
            skills=[],
            # Owner text like "@/etc/niya/niya.env" must stay text, never be expanded into a file read.
            verbatim_prompts=True,
            settings=json.dumps({"autoCompactEnabled": False}),
            model=self.model,
            effort=self.effort,
            max_turns=self.max_iterations + 5,
            cwd=str(self.cwd),
            env=claude_env(self.config_dir, self.secrets),
            resume=resume,
            stderr=lambda line: log.debug("claude: %s", line),
        )

    async def run_turn(self, items: Sequence[InboundItem]) -> TurnResult:
        if not items:
            raise ValueError("一回合至少要有一則訊息。")
        conv = self.conversation
        turn_id = new_turn_id()
        conv.start_turn(turn_id, source=turn_source(items), ts=self.clock.now())
        start = len(conv.messages)
        conv.add_user_items(turn_id, items)
        if conv.engine is None:
            conv.bind_engine(self.engine)
        await recall_into(self.recall, conv, turn_id, items)
        turn = _Turn(turn_id, ToolContext(session_id=conv.session_id, turn_id=turn_id, now=self.clock.now))
        self._turn = turn
        try:
            client = await self._ensure_client(turn)
            prompt = render_history(conv.messages[start:], conv.tz, conv.recall_blocks)[0]["content"]
            await client.query(prompt)
            async for message in client.receive_response():
                self._handle(turn, message)
            self._flush(turn)
            # max_turns can stop Claude Code between a tool call and its result.
            conv.close_dangling_tool_calls(turn_id, "錯誤：這個呼叫沒有執行。", ts=self.clock.now())
            conv.finish_turn(turn_id, ts=self.clock.now())
            if turn.error:
                raise LLMError(turn.error, retryable=turn.rate_limit is not None)
            return TurnResult(turn_id=turn_id, text=turn.text, iterations=turn.iterations, prompt_tokens=self.last_prompt_tokens, context_full=turn.context_full)
        except asyncio.CancelledError:
            self._end_abruptly(turn, "[已中斷]")
            with suppress(Exception):
                await self._disconnect()
            raise
        except ClaudeSDKError as exc:
            self._end_abruptly(turn, "錯誤：Claude Code 中斷了，這個呼叫的結果不明。")
            await self._disconnect()
            raise LLMError(f"Claude Code 執行失敗：{exc}", retryable=True) from exc
        finally:
            self._turn = None
            self._stash.clear()
            if self.on_turn_end:
                self.on_turn_end(conv.session_id)

    async def set_model(self, model: str) -> None:
        self.model = model
        if self._client is not None:
            await self._client.set_model(model)

    async def set_effort(self, effort: str | None) -> None:
        effort = effort_for(ENGINE, effort or "")
        if effort != self.effort:
            self.effort = effort
            # Effort is fixed when Claude Code starts; the next turn reconnects and resumes the same session.
            await self._disconnect()

    async def aclose(self) -> None:
        await self._disconnect()

    async def _ensure_client(self, turn: _Turn) -> SDKClient:
        conv = self.conversation
        if self._client is not None and self._client_session == conv.session_id:
            return self._client
        await self._disconnect()
        resume = conv.engine_session_id if conv.engine == self.engine else None
        try:
            client = await self._connect(resume)
        except ProcessError as exc:
            if resume is None or MISSING_SESSION not in str(exc):
                raise
            # The transcript is gone (deleted, another machine, a changed config dir): go on in a fresh one.
            log.warning("Claude Code session %s not found; starting a new one", resume)
            conv.add_system_line(turn.turn_id, LOST_CONTEXT_NOTE, ts=self.clock.now())
            client = await self._connect(None)
        self._client, self._client_session = client, conv.session_id
        return client

    async def _connect(self, resume: str | None) -> SDKClient:
        client = self.client_factory(self.options(resume))
        await client.connect()
        return client

    async def _disconnect(self) -> None:
        client, self._client, self._client_session = self._client, None, None
        if client is not None:
            await client.disconnect()

    def _end_abruptly(self, turn: _Turn, text: str) -> None:
        self._flush(turn)
        self.conversation.close_dangling_tool_calls(turn.turn_id, text, ts=self.clock.now())
        self.conversation.finish_turn(turn.turn_id, ts=self.clock.now())

    def _remember_session(self, engine_session_id: str | None) -> None:
        conv = self.conversation
        if engine_session_id and engine_session_id != conv.engine_session_id:
            conv.bind_engine(self.engine, engine_session_id)

    def _handle(self, turn: _Turn, message: Any) -> None:
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                self._remember_session(message.data.get("session_id"))
        elif isinstance(message, AssistantMessage):
            if message.parent_tool_use_id:
                return
            self._remember_session(message.session_id)
            if message.error:
                # Claude Code reports API failures as a synthetic assistant message; it is not niya speaking.
                turn.error = turn.error or ERROR_TEXT.get(message.error) or _block_text_of(message) or message.error
                return
            if turn.pending is not None and turn.pending.message_id != message.message_id:
                self._flush(turn)
            if turn.pending is None:
                turn.pending = _Pending(message.message_id, message.model)
            for block in message.content:
                if isinstance(block, TextBlock):
                    turn.pending.texts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    name = block.name.removeprefix(TOOL_PREFIX)
                    call = ToolCall(id=block.id, name=name, arguments=json.dumps(block.input, ensure_ascii=False))
                    turn.pending.calls.append(call)
                    turn.calls[block.id] = (call, _args_key(block.input))
            if message.usage:
                self.last_prompt_tokens = _prompt_tokens(message.usage)
                if over(self.last_prompt_tokens, self.context_limit, HARD) and not turn.context_full:
                    turn.context_full = True
                    turn.cap_text = CONTEXT_FULL_TOOL_TEXT
        elif isinstance(message, UserMessage):
            if message.parent_tool_use_id:
                return
            self._flush(turn)
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        self._record_tool_result(turn, block)
        elif isinstance(message, RateLimitEvent):
            info = message.rate_limit_info
            if info.status == "rejected":
                turn.rate_limit = info
            if self.on_rate_limit:
                self.on_rate_limit(info)
        elif isinstance(message, ResultMessage):
            self._flush(turn)
            self._remember_session(message.session_id)
            self._record_call(message)
            if message.is_error and message.subtype != "error_max_turns":
                if turn.rate_limit is not None:
                    turn.error = describe_rate_limit(turn.rate_limit, self.conversation.tz)
                else:
                    turn.error = turn.error or "；".join(message.errors or []) or message.result or message.subtype

    def _flush(self, turn: _Turn) -> None:
        pending, turn.pending = turn.pending, None
        if pending is None or not (pending.texts or pending.calls):
            return
        text = "\n\n".join(t for t in pending.texts if t.strip()) or None
        result = ChatResult(content=text, tool_calls=tuple(pending.calls), usage=Usage(), model=pending.model)
        stored = self.conversation.add_assistant(turn.turn_id, result, ts=self.clock.now())
        if stored.content:
            turn.text = stored.content
        if pending.calls:
            turn.iterations += 1
            if turn.iterations >= self.max_iterations and turn.cap_text is None:
                turn.cap_text = CAP_TOOL_TEXT

    def _record_tool_result(self, turn: _Turn, block: ToolResultBlock) -> None:
        call, key = turn.calls.get(block.tool_use_id) or (ToolCall(id=block.tool_use_id, name="unknown", arguments="{}"), "")
        text = _block_text(block.content)
        stash = self._stash.get((call.name, key))
        outcome = stash.popleft() if stash else None
        full = outcome.full_content if outcome is not None and outcome.content == text else text
        result = ToolOutcome(call=call, content=text, full_content=full, is_error=bool(block.is_error))
        self.conversation.add_tool_result(turn.turn_id, result, ts=self.clock.now())

    def _record_call(self, message: ResultMessage) -> None:
        if self.recorder is None:
            return
        usage = message.usage or {}
        self.recorder(
            CallRecord(
                task="chat",
                model=f"{ENGINE}:{self.model}",
                usage=Usage(
                    prompt_tokens=_prompt_tokens(usage),
                    completion_tokens=int(usage.get("output_tokens") or 0),
                    cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
                    # What the same tokens would cost on the API; a subscription is billed by its limits instead.
                    cost_usd=message.total_cost_usd,
                ),
                latency_ms=message.duration_api_ms,
                ok=not message.is_error,
                error=_result_error(message) if message.is_error else None,
            )
        )

    def _sdk_tool(self, tool: Tool) -> SdkMcpTool[Any]:
        schema = {"type": "object", "properties": {}, **tool.parameters}

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return await self._call_tool(tool.name, args)

        return SdkMcpTool(
            name=tool.name,
            description=tool.description,
            input_schema=schema,
            handler=handler,
            annotations=ToolAnnotations(readOnlyHint=tool.parallel_safe, maxResultSizeChars=tool.max_result_chars + 1000),
        )

    async def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        turn = self._turn
        if turn is None:
            return _tool_result("錯誤：目前沒有進行中的回合。", is_error=True)
        if turn.cap_text is not None:
            return _tool_result(turn.cap_text, is_error=True)
        call = ToolCall(id=f"cc-{secrets.token_hex(4)}", name=name, arguments=json.dumps(args, ensure_ascii=False))
        [outcome] = await self.tools.execute([call], turn.ctx)
        # The handler never learns Claude's tool_use_id; the stream's tool_result is matched back by name + args.
        self._stash[(name, _args_key(args))].append(outcome)
        return _tool_result(outcome.content, is_error=outcome.is_error)


def _tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def _result_error(message: ResultMessage) -> str:
    # An API failure still ends the query loop normally, so its subtype is "success".
    if message.subtype != "success":
        return message.subtype
    return f"api_error {message.api_error_status}" if message.api_error_status else "api_error"


def _block_text_of(message: AssistantMessage) -> str:
    return "\n".join(block.text for block in message.content if isinstance(block, TextBlock)).strip()


def describe_rate_limit(info: RateLimitInfo, tz: Any) -> str:
    label = LIMIT_LABELS.get(info.rate_limit_type or "", "用量額度")
    when = f"，{datetime.fromtimestamp(info.resets_at, tz):%m/%d %H:%M} 重置" if info.resets_at else ""
    return f"Claude 的{label}用完了{when}。"
