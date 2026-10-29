"""Alarms niya sets for itself. When one goes off it lands in the inbox with top priority, so niya wakes up
for a turn to remind the owner or to go and do the thing.

An alarm that comes due while niya is not running still fires late within its grace window (remind: 6 h,
task: 24 h); past that it is marked missed and niya is told, so it can own up to the owner.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from niya.agent.render import format_ts
from niya.core.clock import Clock
from niya.core.db import Database
from niya.core.inbox import Inbox

log = logging.getLogger(__name__)

PURPOSES = ("remind", "task")
GRACE_S = {"remind": 6 * 3600, "task": 24 * 3600}
MIN_REPEAT_S = 600
MAX_ACTIVE = 100
TICK_S = 30
PURPOSE_LABELS = {"remind": "提醒主人", "task": "該做的事"}
_DATETIME = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})\s*$")


class AlarmError(ValueError):
    pass


@dataclass(frozen=True)
class Alarm:
    id: int
    title: str
    purpose: str
    note: str | None
    cron_expr: str | None
    next_fire_at: float | None
    wake_from_sleep: bool
    late_grace_s: float
    max_fires: int | None
    created_at: float
    status: str
    fire_count: int


def parse_datetime(text: str, tz: ZoneInfo) -> float:
    match = _DATETIME.match(text)
    if not match:
        raise AlarmError(f"看不懂的時間：{text}（請用 YYYY-MM-DD HH:MM）")
    try:
        return datetime(*(int(g) for g in match.groups()), tzinfo=tz).timestamp()
    except ValueError:
        raise AlarmError(f"不存在的時間：{text}") from None


def next_cron(expr: str, after: float, tz: ZoneInfo) -> float:
    return croniter(expr, datetime.fromtimestamp(after, tz)).get_next(datetime).timestamp()


def _row(r) -> Alarm:
    return Alarm(
        id=r["id"],
        title=r["title"],
        purpose=r["purpose"],
        note=r["note"],
        cron_expr=r["cron_expr"],
        next_fire_at=r["next_fire_at"],
        wake_from_sleep=bool(r["wake_from_sleep"]),
        late_grace_s=r["late_grace_s"],
        max_fires=r["max_fires"],
        created_at=r["created_at"],
        status=r["status"],
        fire_count=r["fire_count"],
    )


class Alarms:
    def __init__(self, db: Database, clock: Clock, tz: ZoneInfo) -> None:
        self.db = db
        self.clock = clock
        self.tz = tz
        self._changed = asyncio.Event()

    def set(
        self,
        *,
        title: str,
        at: float | None = None,
        cron: str | None = None,
        purpose: str = "remind",
        note: str | None = None,
        wake: bool | None = None,
        max_fires: int | None = None,
        created_by: str = "niya",
        session_id: str | None = None,
    ) -> Alarm:
        title = title.strip()
        if not title:
            raise AlarmError("鬧鐘要有標題。")
        if purpose not in PURPOSES:
            raise AlarmError(f"purpose 只能是 {' 或 '.join(PURPOSES)}。")
        now = self.clock.now()
        if cron:
            if not croniter.is_valid(cron):
                raise AlarmError(f"看不懂的 cron：{cron}（五個欄位：分 時 日 月 週）")
            start = max(now, at) if at is not None else now
            first = next_cron(cron, start, self.tz)
            if next_cron(cron, first, self.tz) - first < MIN_REPEAT_S:
                raise AlarmError("重複鬧鐘的間隔至少要 10 分鐘。")
        elif at is not None:
            if at <= now:
                raise AlarmError(f"{format_ts(at, self.tz)} 已經過了，鬧鐘只能設在未來。")
            first = at
        else:
            raise AlarmError("請給時間（at 或 in_minutes）或重複規則（repeat）。")
        active = self.db.one("SELECT COUNT(*) AS n FROM alarms WHERE status = 'scheduled'")["n"]
        if active >= MAX_ACTIVE:
            raise AlarmError(f"同時最多 {MAX_ACTIVE} 個鬧鐘，請先取消一些。")
        cursor = self.db.execute(
            "INSERT INTO alarms(title, purpose, note, cron_expr, next_fire_at, wake_from_sleep, late_grace_s, max_fires, created_by, created_at, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                purpose,
                (note or "").strip() or None,
                cron or None,
                first,
                int(purpose == "remind" if wake is None else wake),
                GRACE_S[purpose],
                max_fires,
                created_by,
                now,
                session_id,
            ),
        )
        self._changed.set()
        return self.get(cursor.lastrowid)

    def get(self, alarm_id: int) -> Alarm | None:
        row = self.db.one("SELECT * FROM alarms WHERE id = ?", (alarm_id,))
        return _row(row) if row else None

    def active(self) -> list[Alarm]:
        return [_row(r) for r in self.db.query("SELECT * FROM alarms WHERE status = 'scheduled' ORDER BY next_fire_at, id")]

    def _scheduled(self, alarm_id: int) -> Alarm:
        alarm = self.get(alarm_id)
        if alarm is None:
            raise AlarmError(f"沒有 #{alarm_id} 這個鬧鐘。")
        if alarm.status != "scheduled":
            raise AlarmError(f"鬧鐘 #{alarm_id} 已經不在排程上了（{alarm.status}）。")
        return alarm

    def cancel(self, alarm_id: int) -> Alarm:
        self._scheduled(alarm_id)
        self.db.execute("UPDATE alarms SET status = 'cancelled' WHERE id = ?", (alarm_id,))
        return self.get(alarm_id)

    def snooze(self, alarm_id: int, until: float) -> Alarm:
        self._scheduled(alarm_id)
        if until <= self.clock.now():
            raise AlarmError("延後的時間要在未來。")
        self.db.execute("UPDATE alarms SET next_fire_at = ? WHERE id = ?", (until, alarm_id))
        self._changed.set()
        return self.get(alarm_id)

    def describe(self, alarm: Alarm) -> str:
        when = format_ts(alarm.next_fire_at, self.tz) if alarm.next_fire_at else "—"
        repeat = f"，重複 {alarm.cron_expr}" if alarm.cron_expr else ""
        return f"#{alarm.id} {when} {PURPOSE_LABELS[alarm.purpose]}：{alarm.title}{repeat}"

    def render(self, alarm: Alarm, now: float) -> str:
        """The alarm line niya sees."""
        origin = f"{datetime.fromtimestamp(alarm.created_at, self.tz):%m/%d %H:%M} 設定"
        details = [f"#{alarm.id}", f"重複 {alarm.cron_expr}" if alarm.cron_expr else origin]
        late = (now - alarm.next_fire_at) // 60 if alarm.next_fire_at else 0
        if late >= 2:
            details.append(f"遲了 {int(late)} 分鐘")
        text = f"⏰ {PURPOSE_LABELS[alarm.purpose]}：{alarm.title}（{'，'.join(details)}）"
        return text + (f"\n備註：{alarm.note}" if alarm.note else "")

    def fire_due(self, inbox: Inbox, now: float) -> list[int]:
        """Send every due alarm to the inbox (or report it missed) and schedule its next ring."""
        fired: list[int] = []
        due = self.db.query("SELECT * FROM alarms WHERE status = 'scheduled' AND next_fire_at <= ? ORDER BY next_fire_at, id", (now,))
        for row in due:
            alarm = _row(row)
            missed = now - alarm.next_fire_at > alarm.late_grace_s
            if missed:
                inbox.put(
                    "system",
                    f"鬧鐘 #{alarm.id}「{alarm.title}」原本 {format_ts(alarm.next_fire_at, self.tz)} 要響，"
                    "那時 niya 沒在運作，已經錯過了。必要的話告訴主人。",
                    ts=now,
                    meta={"alarm_id": alarm.id, "missed": True},
                )
            else:
                inbox.put("alarm", self.render(alarm, now), ts=now, meta={"alarm_id": alarm.id})
                fired.append(alarm.id)
            count = alarm.fire_count + (0 if missed else 1)
            if alarm.cron_expr and (alarm.max_fires is None or count < alarm.max_fires):
                status, next_at = "scheduled", next_cron(alarm.cron_expr, now, self.tz)
            else:
                status, next_at = ("missed" if missed else "fired"), alarm.next_fire_at
            self.db.execute(
                "UPDATE alarms SET status = ?, next_fire_at = ?, fire_count = ?, last_fired_at = ? WHERE id = ?",
                (status, next_at, count, None if missed else now, alarm.id),
            )
        return fired

    def next_due(self) -> float | None:
        row = self.db.one("SELECT MIN(next_fire_at) AS at FROM alarms WHERE status = 'scheduled'")
        return row["at"] if row else None

    async def run(self, inbox: Inbox, on_fire: Callable[[list[Alarm]], Awaitable[None]] | None = None) -> None:
        """Ring alarms on time; wakes early whenever an alarm is added or moved."""
        while True:
            try:
                fired = self.fire_due(inbox, self.clock.now())
                if fired and on_fire is not None:
                    await on_fire([alarm for alarm in map(self.get, fired) if alarm is not None])
            except Exception:
                log.exception("alarm tick failed")
            due = self.next_due()
            wait = TICK_S if due is None else min(TICK_S, max(0.2, due - self.clock.now()))
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), wait)
            except TimeoutError:
                pass
