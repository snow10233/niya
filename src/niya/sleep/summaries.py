"""Night-only stages: the day summary (layered time memory) and the dream journal."""

import json
import logging
from datetime import datetime

from niya.agent.render import format_ts
from niya.memory.curve import DAY
from niya.memory.engine import Memory, MemoryEngine
from niya.sleep.llm import Completer, parse_json
from niya.sleep.prompts import DAY_SUMMARY_SYSTEM, DREAM_SYSTEM

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
