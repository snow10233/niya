from typing import Any

from niya.agent.render import format_ts
from niya.alarms import PURPOSE_LABELS, AlarmError, Alarms, parse_datetime
from niya.tools.registry import Tool, ToolContext, ToolError

DESCRIPTION = (
    "設鬧鐘、看鬧鐘、取消或延後。時間到了你會收到一則 alarm: 訊息並醒來處理。"
    "purpose=remind：提醒主人某件事（預設，睡覺時也會叫醒你）；purpose=task：時間到了你自己去做某件事，例如定期檢查。"
    "時間用 at（YYYY-MM-DD HH:MM，你所在的時區）或 in_minutes；重複用 repeat（cron 五個欄位：分 時 日 月 週，例如 0 9 * * 1 是每週一 09:00），間隔至少 10 分鐘。"
    "note 寫下時間到了需要知道的細節。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["set", "list", "cancel", "snooze"]},
        "title": {"type": "string", "description": "set：要提醒或要做的事，一句話"},
        "at": {"type": "string", "description": "set / snooze：YYYY-MM-DD HH:MM"},
        "in_minutes": {"type": "integer", "description": "set / snooze：幾分鐘後"},
        "repeat": {"type": "string", "description": "set：cron 重複規則"},
        "purpose": {"type": "string", "enum": ["remind", "task"], "default": "remind"},
        "note": {"type": "string", "description": "set：備註"},
        "wake": {"type": "boolean", "description": "set：睡覺時要不要叫醒你（remind 預設是，task 預設否）"},
        "max_fires": {"type": "integer", "description": "set：重複鬧鐘最多響幾次"},
        "id": {"type": "integer", "description": "cancel / snooze：鬧鐘編號"},
    },
    "required": ["action"],
}


def alarm_tools(alarms: Alarms) -> list[Tool]:
    def when(args: dict[str, Any], now: float) -> float | None:
        if args.get("at"):
            return parse_datetime(str(args["at"]), alarms.tz)
        if args.get("in_minutes") is not None:
            minutes = int(args["in_minutes"])
            if minutes <= 0:
                raise AlarmError("in_minutes 要大於 0。")
            return now + minutes * 60
        return None

    async def alarm(args: dict[str, Any], ctx: ToolContext) -> str:
        action = args.get("action")
        now = ctx.now()
        try:
            if action == "set":
                created = alarms.set(
                    title=str(args.get("title") or ""),
                    at=when(args, now),
                    cron=(args.get("repeat") or "").strip() or None,
                    purpose=args.get("purpose") or "remind",
                    note=args.get("note"),
                    wake=args.get("wake"),
                    max_fires=args.get("max_fires"),
                    session_id=ctx.session_id,
                )
                when_text = format_ts(created.next_fire_at, alarms.tz)
                if created.cron_expr:
                    return f"已設定重複鬧鐘 #{created.id}（{created.cron_expr}）：下次 {when_text} {PURPOSE_LABELS[created.purpose]}「{created.title}」。"
                return f"已設定鬧鐘 #{created.id}：{when_text} {PURPOSE_LABELS[created.purpose]}「{created.title}」。"
            if action == "list":
                active = alarms.active()
                return "\n".join(["排程中的鬧鐘："] + [f"- {alarms.describe(a)}" for a in active]) if active else "目前沒有鬧鐘。"
            if action in ("cancel", "snooze"):
                if args.get("id") is None:
                    raise AlarmError("請給鬧鐘編號 id。")
                if action == "cancel":
                    cancelled = alarms.cancel(int(args["id"]))
                    return f"已取消鬧鐘 #{cancelled.id}「{cancelled.title}」。"
                until = when(args, now)
                if until is None:
                    raise AlarmError("請給延後到的時間（at 或 in_minutes）。")
                moved = alarms.snooze(int(args["id"]), until)
                return f"鬧鐘 #{moved.id} 延到 {format_ts(moved.next_fire_at, alarms.tz)}。"
        except AlarmError as exc:
            raise ToolError(str(exc)) from None
        raise ToolError(f"未知的 action：{action}")

    return [Tool(name="alarm", description=DESCRIPTION, parameters=PARAMETERS, handler=alarm, toolset="alarm", timeout_s=30, max_result_chars=6000)]
