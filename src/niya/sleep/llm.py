"""One-shot completions for sleep work. Sleep never hands the model any tools."""

import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKError, ResultMessage, TextBlock, query

from niya.llm.claude_cli import claude_env
from niya.llm.openrouter import CallRecord, LLMError, OpenRouterClient, Recorder, Usage


class Completer(Protocol):
    async def complete(self, *, system: str, prompt: str, task: str) -> str: ...


class OpenRouterCompleter:
    def __init__(self, client: OpenRouterClient, model: str, effort: str | None = None) -> None:
        self.client = client
        self.model = model
        self.effort = effort

    async def complete(self, *, system: str, prompt: str, task: str) -> str:
        result = await self.client.chat(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            task=task,
            reasoning={"effort": self.effort} if self.effort else None,
        )
        return result.content or ""


class DeepSeekCompleter(OpenRouterCompleter):
    """DeepSeek's own API: thinking has to be switched on explicitly, and it only knows low, high and max."""

    async def complete(self, *, system: str, prompt: str, task: str) -> str:
        extra: dict[str, Any] = {"thinking": {"type": "enabled"}}
        if self.effort:
            extra["reasoning_effort"] = {"none": "low", "minimal": "low", "medium": "high", "xhigh": "max"}.get(self.effort, self.effort)
        result = await self.client.chat(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            task=task,
            extra=extra,
        )
        return result.content or ""


class ClaudeCodeCompleter:
    """Each call is a fresh Claude Code session with no tools and a single turn."""

    def __init__(self, model: str, *, config_dir: Path, cwd: Path, secrets: Mapping[str, str], effort: str | None = None, recorder: Recorder | None = None) -> None:
        self.model = model
        self.config_dir = config_dir.resolve()
        self.cwd = cwd.resolve()
        self.secrets = dict(secrets)
        self.effort = effort
        self.recorder = recorder

    async def complete(self, *, system: str, prompt: str, task: str) -> str:
        options = ClaudeAgentOptions(
            system_prompt=system,
            tools=[],
            strict_mcp_config=True,
            permission_mode="dontAsk",
            setting_sources=[],
            skills=[],
            verbatim_prompts=True,
            settings=json.dumps({"autoCompactEnabled": False}),
            model=self.model,
            effort=self.effort,
            max_turns=1,
            cwd=str(self.cwd),
            env=claude_env(self.config_dir, self.secrets),
        )
        texts: list[str] = []
        started = time.monotonic()
        result: ResultMessage | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage) and not message.error:
                    texts += [block.text for block in message.content if isinstance(block, TextBlock)]
                elif isinstance(message, ResultMessage):
                    result = message
        except ClaudeSDKError as exc:
            raise LLMError(f"Claude Code 睡眠整理失敗：{exc}", retryable=True) from exc
        if self.recorder and result is not None:
            usage = result.usage or {}
            prompt_tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            self.recorder(
                CallRecord(
                    task=task,
                    model=f"claude:{self.model}",
                    usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=int(usage.get("output_tokens") or 0), cost_usd=result.total_cost_usd),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    ok=not result.is_error,
                    error=result.subtype if result.is_error else None,
                )
            )
        if result is not None and result.is_error:
            raise LLMError(f"Claude Code 睡眠整理失敗：{'；'.join(result.errors or []) or result.result or result.subtype}", retryable=True)
        return "\n".join(texts) or (result.result if result else "") or ""


def parse_json(text: str) -> Any:
    """The first JSON object in a reply, tolerating code fences and chatter around it.

    The object is tried before any fence: its strings may hold markdown code blocks of their own."""
    decoder = json.JSONDecoder()
    candidates = [text.strip()]
    if fenced := re.search(r"```(?:json)?\s*(.*?)```", text, re.S):
        candidates.append(fenced.group(1).strip())
    error: ValueError = ValueError("回覆裡沒有 JSON")
    for candidate in candidates:
        start = candidate.find("{")
        if start < 0:
            continue
        try:
            value, _ = decoder.raw_decode(candidate[start:])
        except ValueError as exc:
            error = exc
            continue
        return value
    raise error
