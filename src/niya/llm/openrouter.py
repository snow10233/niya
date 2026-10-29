import asyncio
import copy
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def to_api(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.arguments}}


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class ChatResult:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    usage: Usage
    model: str
    finish_reason: str | None = None
    reasoning_details: list[Any] | None = None


@dataclass(frozen=True)
class CallRecord:
    task: str
    model: str
    usage: Usage
    latency_ms: int
    ok: bool
    error: str | None = None


Recorder = Callable[[CallRecord], None]


def apply_cache_control(model: str, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic models only cache with explicit breakpoints: mark the system prompt and the newest text message."""
    out = [dict(m) for m in messages]
    if not model.startswith("anthropic/"):
        return out
    targets = [0] if out and out[0].get("role") == "system" else []
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") in ("user", "assistant") and out[i].get("content"):
            if i not in targets:
                targets.append(i)
            break
    for i in targets:
        content = out[i].get("content")
        if isinstance(content, str):
            out[i]["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(content, list):
            # A message with pictures: the breakpoint goes on its last part.
            out[i]["content"] = [*content[:-1], {**content[-1], "cache_control": {"type": "ephemeral"}}]
    return out


def _parse_usage(data: dict[str, Any]) -> Usage:
    usage = data.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cost = usage.get("cost")
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cached_tokens=int(details.get("cached_tokens") or 0),
        cost_usd=float(cost) if cost is not None else None,
    )


def _parse_tool_calls(raw: Sequence[dict[str, Any]] | None) -> tuple[ToolCall, ...]:
    calls = []
    for item in raw or ():
        function = item.get("function") or {}
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        calls.append(ToolCall(id=item.get("id") or f"call_{uuid.uuid4().hex[:12]}", name=function.get("name", ""), arguments=arguments))
    return tuple(calls)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http: httpx.AsyncClient | None = None,
        recorder: Recorder | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout: float = 180.0,
        name: str = "OpenRouter",
        key_name: str = "OPENROUTER_API_KEY",
    ) -> None:
        if not api_key:
            raise ValueError(f"請先設定 {key_name}。")
        # Any OpenAI-compatible endpoint works (DeepSeek's own API does); `name` only shows in errors.
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=timeout)
        self.recorder = recorder
        self.max_retries = max_retries
        self.sleep = sleep
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "niya"}
        self._context_lengths: dict[str, int | None] = {}

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
        extra: dict[str, Any] | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {"model": model, "messages": apply_cache_control(model, messages), "usage": {"include": True}}
        if reasoning:
            body["reasoning"] = reasoning
        if extra:
            body.update(extra)
        if tools:
            body["tools"] = list(tools)
            body["parallel_tool_calls"] = True
        if tool_choice:
            body["tool_choice"] = tool_choice
        if max_tokens:
            body["max_tokens"] = max_tokens
        data = await self._post("/chat/completions", body, task=task, model=model)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name} 沒有回傳任何 choices。", retryable=True)
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content")
        return ChatResult(
            content=content if isinstance(content, str) else None,
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
            usage=_parse_usage(data),
            model=data.get("model", model),
            finish_reason=choice.get("finish_reason"),
            reasoning_details=copy.deepcopy(message.get("reasoning_details")) or None,
        )

    async def chat_raw(self, body: dict[str, Any], *, task: str) -> dict[str, Any]:
        """The whole response, for outputs ChatResult has no place for (generated images)."""
        return await self._post("/chat/completions", body, task=task, model=body["model"])

    async def embed(self, *, model: str, texts: Sequence[str], dimensions: int | None = None) -> list[list[float]]:
        body: dict[str, Any] = {"model": model, "input": list(texts)}
        if dimensions:
            body["dimensions"] = dimensions
        data = await self._post("/embeddings", body, task="embed", model=model)
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        if len(items) != len(texts):
            raise LLMError(f"embedding 數量不符：送出 {len(texts)} 筆，收到 {len(items)} 筆。")
        return [item["embedding"] for item in items]

    async def context_length(self, model: str) -> int | None:
        if model not in self._context_lengths:
            response = await self.http.get(f"{self.base_url}/models", headers=self._headers)
            response.raise_for_status()
            for item in response.json().get("data") or []:
                self._context_lengths[item.get("id", "")] = item.get("context_length")
            self._context_lengths.setdefault(model, None)
        return self._context_lengths[model]

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _post(self, path: str, body: dict[str, Any], *, task: str, model: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            delay = min(2.0**attempt, 30.0)
            try:
                response = await self.http.post(url, json=body, headers=self._headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                error = LLMError(f"連線 {self.name} 失敗：{type(exc).__name__}", retryable=True)
            else:
                error = self._check(response)
                if error is None:
                    data = response.json()
                    self._record(task, model, _parse_usage(data), started, ok=True)
                    return data
                delay = _retry_after(response) or delay
            if not error.retryable or attempt == self.max_retries:
                self._record(task, model, Usage(), started, ok=False, error=str(error))
                raise error
            await self.sleep(delay)
        raise AssertionError("unreachable")

    def _check(self, response: httpx.Response) -> LLMError | None:
        if response.status_code in RETRY_STATUS:
            return LLMError(f"{self.name} 暫時無法使用（HTTP {response.status_code}）", retryable=True)
        if response.status_code >= 400:
            return LLMError(f"{self.name} 回應 HTTP {response.status_code}：{response.text[:300]}")
        try:
            data = response.json()
        except ValueError:
            return LLMError(f"{self.name} 回傳的不是 JSON。", retryable=True)
        error = data.get("error") if isinstance(data, dict) else None
        if error is None:
            choices = data.get("choices") or []
            error = choices[0].get("error") if choices and isinstance(choices[0], dict) else None
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else str(error)
            retryable = isinstance(code, int) and code in RETRY_STATUS
            return LLMError(f"{self.name} 回傳錯誤：{message}", retryable=retryable)
        return None

    def _record(self, task: str, model: str, usage: Usage, started: float, *, ok: bool, error: str | None = None) -> None:
        if self.recorder:
            latency = int((time.monotonic() - started) * 1000)
            self.recorder(CallRecord(task=task, model=model, usage=usage, latency_ms=latency, ok=ok, error=error))


class OpenRouterEmbedder:
    """Embeddings for memory. `model` names the vector space (model plus dimensions) stored with each vector."""

    def __init__(self, client: OpenRouterClient, model: str, dimensions: int | None = None) -> None:
        self.client = client
        self.name = model
        self.dimensions = dimensions
        self.model = f"{model}@{dimensions}" if dimensions else model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return await self.client.embed(model=self.name, texts=texts, dimensions=self.dimensions)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    context_length: int | None
    prompt_price: float | None  # USD per 1M tokens
    completion_price: float | None
    tools: bool


def _per_million(value: Any) -> float | None:
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


async def list_models(*, base_url: str = DEFAULT_BASE_URL, http: httpx.AsyncClient | None = None) -> list[ModelInfo]:
    """OpenRouter's public model catalog; no key needed."""
    client = http or httpx.AsyncClient(timeout=30)
    try:
        response = await client.get(f"{base_url.rstrip('/')}/models")
        response.raise_for_status()
        items = response.json().get("data") or []
    finally:
        if http is None:
            await client.aclose()
    return [
        ModelInfo(
            id=item["id"],
            name=item.get("name") or item["id"],
            context_length=item.get("context_length"),
            prompt_price=_per_million((item.get("pricing") or {}).get("prompt")),
            completion_price=_per_million((item.get("pricing") or {}).get("completion")),
            tools="tools" in (item.get("supported_parameters") or ()),
        )
        for item in items
        if item.get("id")
    ]


def db_recorder(db: Any, clock: Any) -> Recorder:
    def record(call: CallRecord) -> None:
        db.execute(
            "INSERT INTO llm_calls(ts, task, model, prompt_tokens, completion_tokens, cached_tokens, cost_usd, latency_ms, ok, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                clock.now(),
                call.task,
                call.model,
                call.usage.prompt_tokens,
                call.usage.completion_tokens,
                call.usage.cached_tokens,
                call.usage.cost_usd,
                call.latency_ms,
                int(call.ok),
                call.error,
            ),
        )

    return record
