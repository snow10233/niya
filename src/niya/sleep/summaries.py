"""Night-only stages: the day summary (layered time memory) and the dream journal."""

import json
import logging
from datetime import date, datetime, timedelta

from niya.agent.render import format_ts
from niya.memory.curve import DAY
from niya.memory.engine import Memory, MemoryEngine
from niya.sleep.llm import Completer, parse_json
from niya.sleep.prompts import DAY_SUMMARY_SYSTEM, DREAM_SYSTEM, PERIOD_SUMMARY_SYSTEM

log = logging.getLogger(__name__)


async def day_summary(engine: MemoryEngine, completer: Completer, *, since: float) -> Memory | None:
    """One diary entry for everything since the last night's wake; its events become part_of it."""
    now = engine.clock.now()
    rows = engine.db.query(
        "SELECT id FROM memories WHERE kind = 'event' AND state <> 'superseded' AND occurred_start >= ? AND occurred_start < ? ORDER BY occurred_start",
        (since, now),
    )
    events = list(engine.load(r["id"] for r in rows).values())
    if not events:
        return None
    listing = "\n".join(f"- [{format_ts(e.occurred_start, engine.tz)}] {e.content}（重要度 {e.importance}）" for e in events)
    prompt = f"這一天（{format_ts(since, engine.tz)} 到 {format_ts(now, engine.tz)}）的事件：\n{listing}"
    try:
        data = parse_json(await completer.complete(system=DAY_SUMMARY_SYSTEM, prompt=prompt, task="day_summary"))
        text = str(data.get("summary") or "").strip()
        importance = int(data.get("importance") or 0)
    except (ValueError, TypeError) as exc:
        log.warning("day summary unusable: %s", exc)
        text, importance = "；".join(e.content.split("：")[0] for e in events[:6]), 0
    if not text:
        return None
    start = min(e.occurred_start for e in events)
    end = max(e.occurred_end or e.occurred_start for e in events)
    day = datetime.fromtimestamp(start, engine.tz)
    vectors = await engine.embed([text])
    with engine.db.transaction():
        summary = engine.create(
            kind="summary",
            level="day",
            content=f"{day:%m/%d}：{text}",
            importance=max(importance, max(e.importance for e in events)),
            occurred_start=start,
            occurred_end=end,
            precision="day",
            origin="summary",
            vector=vectors[0] if vectors else None,
        )
        for event in events:
            engine.add_link(event.id, summary.id, "part_of")
    return summary


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _start_of(day: date, tz) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()


def _has_summary(engine: MemoryEngine, level: str, start: float, end: float) -> bool:
    return engine.db.one(
        "SELECT 1 FROM memories WHERE kind = 'summary' AND level = ? AND occurred_start >= ? AND occurred_start < ?", (level, start, end)
    ) is not None


def _summaries(engine: MemoryEngine, level: str, start: float, end: float) -> list[Memory]:
    rows = engine.db.query(
        "SELECT id FROM memories WHERE kind = 'summary' AND level = ? AND state <> 'superseded' AND occurred_start >= ? AND occurred_start < ? ORDER BY occurred_start",
        (level, start, end),
    )
    return list(engine.load(r["id"] for r in rows).values())


async def _period_summary(engine: MemoryEngine, completer: Completer, *, level: str, label: str, start: float, end: float, children: list[Memory]) -> Memory | None:
    listing = "\n".join(f"- {c.content}" for c in children)
    period = "這一週" if level == "week" else "這個月"
    try:
        data = parse_json(await completer.complete(system=PERIOD_SUMMARY_SYSTEM.format(period=period), prompt=f"{label}的摘要：\n{listing}", task=f"{level}_summary"))
        text = str(data.get("summary") or "").strip()
        importance = int(data.get("importance") or 0)
    except (ValueError, TypeError) as exc:
        log.warning("%s summary unusable: %s", level, exc)
        return None
    if not text:
        return None
    vectors = await engine.embed([text])
    with engine.db.transaction():
        summary = engine.create(
            kind="summary",
            level=level,
            content=f"{label}：{text}",
            importance=max(importance, max(c.importance for c in children)),
            occurred_start=start,
            occurred_end=end - 1,
            precision="week" if level == "week" else "month",
            origin="summary",
            vector=vectors[0] if vectors else None,
        )
        for child in children:
            engine.add_link(child.id, summary.id, "part_of")
    return summary


async def period_summaries(engine: MemoryEngine, completer: Completer) -> list[Memory]:
    """Summaries for the last complete week and month, if they are still missing (a missed night catches up)."""
    tz = engine.tz
    today = datetime.fromtimestamp(engine.clock.now(), tz).date()
    made: list[Memory] = []
    week_end = _week_start(today)
    week_start = week_end - timedelta(days=7)
    start, end = _start_of(week_start, tz), _start_of(week_end, tz)
    if not _has_summary(engine, "week", start, end) and (days := _summaries(engine, "day", start, end)):
        label = f"{week_start:%m/%d}–{week_end - timedelta(days=1):%m/%d} 這週"
        if summary := await _period_summary(engine, completer, level="week", label=label, start=start, end=end, children=days):
            made.append(summary)
    month_end = _month_start(today)
    month_start = _month_start(month_end - timedelta(days=1))
    start, end = _start_of(month_start, tz), _start_of(month_end, tz)
    if not _has_summary(engine, "month", start, end):
        children = _summaries(engine, "week", start, end) or _summaries(engine, "day", start, end)
        if children:
            label = f"{month_start:%Y 年 %-m 月}"
            if summary := await _period_summary(engine, completer, level="month", label=label, start=start, end=end, children=children):
                made.append(summary)
    return made


def last_night_wake(engine: MemoryEngine) -> float:
    row = engine.db.one("SELECT MAX(ended_at) AS at FROM sleep_runs WHERE kind = 'night' AND status = 'done'")
    return row["at"] if row and row["at"] else engine.clock.now() - DAY


async def dream(completer: Completer, *, run_id: int, titles: list[str], memories: list[str], archived: int, db, now: float) -> str | None:
    if not titles and not memories:
        return None
    prompt = "今晚整理的事件：\n" + "\n".join(f"- {t}" for t in titles[:20])
    if memories:
        prompt += "\n新記住的事：\n" + "\n".join(f"- {m}" for m in memories[:20])
    prompt += f"\n淡忘、封存的記憶：{archived} 條"
    try:
        text = (await completer.complete(system=DREAM_SYSTEM, prompt=prompt, task="dream")).strip()
    except Exception as exc:  # the dream is a nicety; sleep must not fail over it
        log.warning("dream journal failed: %s", exc)
        return None
    if text:
        db.execute(
            "INSERT INTO dream_journal(sleep_run_id, created_at, content, details) VALUES (?, ?, ?, ?)",
            (run_id, now, text, json.dumps({"events": titles, "memories": memories, "archived": archived}, ensure_ascii=False)),
        )
    return text or None
