"""Deliberate recall: tools niya uses to go looking for things that are not in its context."""

import re
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from niya.agent.render import SPEAKER_LABELS, format_ts
from niya.db import Database
from niya.memory.search import search_messages
from niya.tools.registry import Tool, ToolContext, ToolError

CONTEXT_RADIUS = 5
LINE_CHARS = 400

DEEP_RECALL_DESCRIPTION = (
    "搜尋過去的對話紀錄（不含目前這段對話，它已經在你眼前）。"
    "用關鍵字查效果最好，例如「咖啡」「nginx 502」，不要整句話。"
    "可以用 since / until 限定時間（YYYY-MM-DD 或 YYYY-MM-DD HH:MM）。"
    "每筆結果都有 ref（msg#123），用 recall_related 可以看那則訊息的前後文。"
)
DEEP_RECALL_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "關鍵字，多個用空白分開"},
        "since": {"type": "string", "description": "從這個時間開始（含），例如 2026-10-01"},
        "until": {"type": "string", "description": "到這個時間為止（含），例如 2026-10-28 18:00"},
        "limit": {"type": "integer", "description": "最多幾筆，預設 10", "default": 10},
    },
    "required": ["query"],
}
RELATED_DESCRIPTION = "從 deep_recall 找到的 ref 往下挖。msg#123：看那則訊息前後的對話。"
RELATED_PARAMETERS = {
    "type": "object",
    "properties": {"ref": {"type": "string", "description": "例如 msg#123"}},
    "required": ["ref"],
}
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


def _clip(text: str, limit: int = LINE_CHARS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _speaker(row: Any) -> str:
    if row["role"] == "assistant":
        return "niya"
    return SPEAKER_LABELS.get(row["speaker"] or "owner", row["speaker"] or "user")


def memory_tools(db: Database, tz: ZoneInfo) -> list[Tool]:
    async def deep_recall(args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能是空的。")
        limit = max(1, min(int(args.get("limit") or 10), 20))
        since = parse_bound(args.get("since"), tz)
        until = parse_bound(args.get("until"), tz, end=True)
        hits = search_messages(db, query, since=since, until=until, exclude_session=ctx.session_id, limit=limit)
        if not hits:
            return f"過去的對話裡找不到「{query}」。可以換個關鍵字，或放寬時間範圍。"
        lines = [f"找到 {len(hits)} 筆以前的對話（都是先前的 session，不在目前的 context 裡）："]
        lines += [f"- msg#{h.id} [{format_ts(h.ts, tz)}] {'niya' if h.role == 'assistant' else SPEAKER_LABELS.get(h.speaker or 'owner', 'user')}: {h.snippet}" for h in hits]
        return "\n".join(lines)

    async def recall_related(args: dict[str, Any], ctx: ToolContext) -> str:
        ref = str(args.get("ref") or "").strip()
        match = re.fullmatch(r"msg#(\d+)", ref)
        if not match:
            if re.fullmatch(r"m\d+", ref):
                raise ToolError("記憶（m 開頭的 ref）還沒啟用，目前只能查 msg#。")
            raise ToolError(f"看不懂的 ref：{ref}（例如 msg#123）")
        target = db.one("SELECT id, session_id FROM messages WHERE id = ?", (int(match.group(1)),))
        if target is None:
            raise ToolError(f"找不到 {ref}。")
        before = db.query(
            "SELECT * FROM messages WHERE session_id = ? AND id <= ? ORDER BY id DESC LIMIT ?",
            (target["session_id"], target["id"], CONTEXT_RADIUS + 1),
        )
        after = db.query(
            "SELECT * FROM messages WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?",
            (target["session_id"], target["id"], CONTEXT_RADIUS),
        )
        lines = [f"{ref} 前後的對話："]
        for row in [*reversed(before), *after]:
            mark = "→ " if row["id"] == target["id"] else "  "
            stamp = format_ts(row["ts"], tz)
            if row["role"] == "tool":
                size = len(row["content"] or "")
                lines.append(f"{mark}msg#{row['id']} [{stamp}]（{row['tool_name'] or '工具'} 的輸出，{size} 字）")
            elif row["content"]:
                lines.append(f"{mark}msg#{row['id']} [{stamp}] {_speaker(row)}: {_clip(row['content'])}")
            else:
                lines.append(f"{mark}msg#{row['id']} [{stamp}] niya 呼叫了工具")
        return "\n".join(lines)

    return [
        Tool(name="deep_recall", description=DEEP_RECALL_DESCRIPTION, parameters=DEEP_RECALL_PARAMETERS, handler=deep_recall, toolset="memory", parallel_safe=True, timeout_s=30, max_result_chars=8000),
        Tool(name="recall_related", description=RELATED_DESCRIPTION, parameters=RELATED_PARAMETERS, handler=recall_related, toolset="memory", parallel_safe=True, timeout_s=30, max_result_chars=8000),
    ]

