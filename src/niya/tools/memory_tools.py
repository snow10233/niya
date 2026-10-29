"""Memory tools: deliberately remembering, and going looking for what is not in the context."""

import re
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from niya.memory.engine import KIND_LABELS, MemoryEngine
from niya.memory.timeparse import parse_time_range
from niya.tools.registry import Tool, ToolContext, ToolError

REMEMBER_DESCRIPTION = (
    "刻意記住一件事：主人的偏好、重要的事實、未來的計畫。白天記住的還不牢，要等睡覺整理才會變成長期記憶；"
    "睡覺時也會自動從對話整理，所以只記真正重要、之後會用到的。"
    "內容要能單獨看懂，相對時間（明天、下週三）要寫成絕對日期，因為過幾天再看就不準了。"
    "一次只記一件事。發生過的事不用記，睡覺時會自動整理成事件。"
    "kind：fact 事實、preference 偏好、intention 計畫（when 填計畫的時間）。"
    "importance 1–10（10 是一輩子都該記得）。source：owner 主人說的、self 你自己觀察或推論的、external 來自網頁等外部內容。"
)
REMEMBER_PARAMETERS = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "要記住的內容，一句完整的話"},
        "kind": {"type": "string", "enum": ["fact", "preference", "intention"], "default": "fact"},
        "importance": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        "source": {"type": "string", "enum": ["owner", "self", "external"], "default": "owner"},
        "when": {"type": "string", "description": "這件事發生或預定的時間：YYYY-MM-DD 或 YYYY-MM-DD HH:MM"},
    },
    "required": ["content"],
}
DEEP_RECALL_DESCRIPTION = (
    "用力回想：搜尋所有記憶（包括已經很淡、要很用力才想得起來的）和以前的對話，結果附精確時間。"
    "用關鍵字查效果最好，例如「咖啡」「nginx 502」，不要整句話。since / until 可限定時間（YYYY-MM-DD 或 YYYY-MM-DD HH:MM）。"
    "結果的 ref（m12 是記憶、msg#123 是原始訊息）可以交給 recall_related 往下挖。"
)
DEEP_RECALL_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "關鍵字，多個用空白分開"},
        "since": {"type": "string", "description": "從這個時間開始（含）"},
        "until": {"type": "string", "description": "到這個時間為止（含）"},
    },
    "required": ["query"],
}
TIMELINE_DESCRIPTION = "翻日記：列出某段時間發生的事和記下的事，附精確時間。since / until 可以是 YYYY-MM-DD，也可以是「上週」「昨天」「10 月」這類說法（只給 since 時代表那一段）。"
TIMELINE_PARAMETERS = {
    "type": "object",
    "properties": {"since": {"type": "string"}, "until": {"type": "string"}},
    "required": ["since"],
}
RELATED_DESCRIPTION = "從一個 ref 往下挖。m12：看這條記憶的來源、相關的記憶和當時的對話；msg#123：看那則訊息前後的對話。"
RELATED_PARAMETERS = {"type": "object", "properties": {"ref": {"type": "string", "description": "例如 m12 或 msg#123"}}, "required": ["ref"]}
_BOUND = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?\s*$")


def parse_bound(text: str | None, tz: ZoneInfo, *, end: bool = False) -> float | None:
    """'2026-10-28' or '2026-10-28 18:00' in niya's timezone; a bare date as `until` covers the whole day."""
    if not text:
        return None
    match = _BOUND.match(text)
    if not match:
        raise ToolError(f"看不懂的時間：{text}（請用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）")
    year, month, day, hour, minute = match.groups()
    try:
        day_start = datetime(int(year), int(month), int(day), tzinfo=tz)
    except ValueError:
        raise ToolError(f"不存在的日期：{text}") from None
    if hour is None:
        moment = day_start + timedelta(days=1) if end else day_start
    else:
        moment = datetime.combine(day_start.date(), time(int(hour), int(minute)), tzinfo=tz)
        if end:
            moment += timedelta(minutes=1)
    return moment.timestamp()


def _span(text: str, now: float, tz: ZoneInfo) -> tuple[float, float]:
    """A date, a date and time, or a phrase like 「上週」."""
    if _BOUND.match(text):
        return parse_bound(text, tz), parse_bound(text, tz, end=True)
    found = parse_time_range(text, now, tz)
    if found is None:
        raise ToolError(f"看不懂的時間：{text}")
    return found


def memory_tools(engine: MemoryEngine) -> list[Tool]:
    tz = engine.tz

    async def remember(args: dict[str, Any], ctx: ToolContext) -> str:
        when_text = args.get("when")
        when = parse_bound(when_text, tz)
        try:
            kind = args.get("kind") or "fact"
            if kind not in ("fact", "preference", "intention"):
                raise ValueError("remember 只記事實、偏好和計畫；發生過的事睡覺時會自動整理。")
            memory, new = await engine.remember(
                str(args.get("content") or ""),
                kind=kind,
                importance=int(args.get("importance") or 5),
                trust=args.get("source") or "owner",
                occurred_start=when,
                precision=None if when is None else "minute" if ":" in str(when_text) else "day",
                session_id=ctx.session_id,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        if not new:
            return f"這件事已經記得了（m{memory.id}）：{memory.content}"
        return f"記住了（m{memory.id}，{KIND_LABELS[memory.kind]}，重要度 {memory.importance}）。睡覺整理後才會變成長期記憶。"

    async def deep_recall(args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能是空的。")
        since = parse_bound(args.get("since"), tz)
        until = parse_bound(args.get("until"), tz, end=True)
        return await engine.deep_recall(query, session_id=ctx.session_id, turn_id=ctx.turn_id, since=since, until=until)

    async def recall_timeline(args: dict[str, Any], ctx: ToolContext) -> str:
        now = ctx.now()
        since, until = _span(str(args.get("since") or ""), now, tz)
        if args.get("until"):
            until = _span(str(args["until"]), now, tz)[1]
        if until <= since:
            raise ToolError("until 要在 since 之後。")
        return engine.timeline(since, until, session_id=ctx.session_id, turn_id=ctx.turn_id)

    async def recall_related(args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            return engine.related(str(args.get("ref") or "").strip(), session_id=ctx.session_id, turn_id=ctx.turn_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    options: dict[str, Any] = dict(toolset="memory", timeout_s=30, max_result_chars=8000)
    return [
        Tool(name="remember", description=REMEMBER_DESCRIPTION, parameters=REMEMBER_PARAMETERS, handler=remember, **options),
        Tool(name="deep_recall", description=DEEP_RECALL_DESCRIPTION, parameters=DEEP_RECALL_PARAMETERS, handler=deep_recall, parallel_safe=True, **options),
        Tool(name="recall_timeline", description=TIMELINE_DESCRIPTION, parameters=TIMELINE_PARAMETERS, handler=recall_timeline, parallel_safe=True, **options),
        Tool(name="recall_related", description=RELATED_DESCRIPTION, parameters=RELATED_PARAMETERS, handler=recall_related, parallel_safe=True, **options),
    ]
