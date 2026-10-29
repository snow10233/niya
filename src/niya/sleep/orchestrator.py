"""When niya sleeps, and what a night (or a nap) consists of.

Two scheduled starts a day (night 03:00, nap 13:00); niya wakes as soon as the work is done. An early
sleep because the context filled up stands in for the next scheduled one. While asleep, niya holds the
Brain's lock, so whatever arrives waits in the inbox. /wake or a reminder alarm interrupts: the segment
in progress rolls back, the context is kept, and sleep resumes once things have been quiet for a while.
Only when every segment of the session is consolidated does waking up clear the context.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from niya.agent.render import format_ts
from niya.brain import Brain
from niya.clock import Clock
from niya.db import Database
from niya.memory.engine import MemoryEngine
from niya.memory.episodic import close_idle_segments, close_session_segments
from niya.sleep.llm import Completer
from niya.sleep.nrem import Nrem, backfill_vectors
from niya.sleep.summaries import day_summary, dream, last_night_wake

log = logging.getLogger(__name__)

SLOTS = {"night": (3, 0), "nap": (13, 0)}
SLOT_WINDOW = {"night": 6 * 3600, "nap": 3 * 3600}  # a slot missed by more than this (niya was off) is skipped
MAX_DURATION = {"night": 3 * 3600, "nap": 3600}
RESUME_IDLE = {"alarm": 5 * 60, "owner": 30 * 60, "unfinished": 30 * 60}
KIND_LABELS = {"night": "夜間睡眠", "nap": "午睡"}
BACKUPS_KEPT = 7
TICK_S = 30

Say = Callable[[str], Awaitable[None]]
# (kind, reason, wake line) -> end the old session and start a fresh one.
WakeUp = Callable[[str, str, str], Awaitable[None]]


@dataclass
class RunStats:
    segments: int = 0
    failed: int = 0
    events: int = 0
    memories: int = 0
    reencounters: int = 0
    used: int = 0
    labile: int = 0
    archived: int = 0
    vectors: int = 0
    titles: list[str] = field(default_factory=list)
    new_memories: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    intentions: list[int] = field(default_factory=list)
    summary: bool = False
    finished: bool = False


def slot_id(kind: str, at: datetime) -> str:
    return f"{kind}@{at:%Y-%m-%dT%H:%M}"


class SleepOrchestrator:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        tz: ZoneInfo,
        engine: MemoryEngine,
        completer: Callable[[], Completer],
        brain: Brain,
        current_session: Callable[[], str],
        wake_up: WakeUp,
        notify: Say,
        backups_dir=None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.tz = tz
        self.engine = engine
        self.completer = completer
        self.brain = brain
        self.current_session = current_session
        self.wake_up = wake_up
        self.notify = notify
        self.backups_dir = backups_dir
        self.task: asyncio.Task | None = None
        self.progress: str = ""
        self._wake_reason = "owner"
        for row in db.query("SELECT id, kind FROM sleep_runs WHERE status = 'running'"):
            # niya stopped mid-sleep: that run was interrupted and has to be picked up again.
            db.execute("UPDATE sleep_runs SET status = 'interrupted', ended_at = ? WHERE id = ?", (clock.now(), row["id"]))
            self._set_resume(row["kind"], "unfinished")

    # --- state -------------------------------------------------------------------------------------

    @property
    def asleep(self) -> bool:
        return self.task is not None and not self.task.done()

    def _resume(self) -> dict | None:
        raw = self.db.get_meta("sleep.resume")
        return json.loads(raw) if raw else None

    def _set_resume(self, kind: str, reason: str) -> None:
        self.db.set_meta("sleep.resume", json.dumps({"kind": kind, "idle_s": RESUME_IDLE[reason]}))

    def _clear_resume(self) -> None:
        self.db.execute("DELETE FROM state_meta WHERE key = 'sleep.resume'")

    def _handled(self, slot: str) -> bool:
        return self.db.one("SELECT 1 FROM sleep_runs WHERE slot = ? OR replaced_slot = ?", (slot, slot)) is not None

    def _last_activity(self) -> float:
        row = self.db.one("SELECT MAX(COALESCE(ended_at, started_at)) AS at FROM turns")
        return row["at"] if row and row["at"] else 0.0

    def _last_wake(self) -> float:
        row = self.db.one("SELECT MAX(ended_at) AS at FROM sleep_runs WHERE status = 'done'")
        return row["at"] if row and row["at"] else 0.0

    def _slot_time(self, kind: str, day: datetime) -> datetime:
        hour, minute = SLOTS[kind]
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def due_slot(self, now: float) -> tuple[str, str] | None:
        moment = datetime.fromtimestamp(now, self.tz)
        for kind in SLOTS:
            at = self._slot_time(kind, moment)
            if at > moment:
                at -= timedelta(days=1)
            slot = slot_id(kind, at)
            if now - at.timestamp() <= SLOT_WINDOW[kind] and not self._handled(slot):
                return kind, slot
        return None

    def replaced_slot(self, now: float) -> tuple[str, str]:
        """An early sleep before today's nap counts as the nap; after it, as tonight's night sleep."""
        moment = datetime.fromtimestamp(now, self.tz)
        nap = self._slot_time("nap", moment)
        if moment < nap and not self._handled(slot_id("nap", nap)):
            return "nap", slot_id("nap", nap)
        night = self._slot_time("night", moment)
        if night <= moment:
            night += timedelta(days=1)
        return "night", slot_id("night", night)

    def next_slot(self, now: float) -> str:
        moment = datetime.fromtimestamp(now, self.tz)
        upcoming = []
        for kind in SLOTS:
            for days in range(3):
                at = self._slot_time(kind, moment) + timedelta(days=days)
                if at > moment and not self._handled(slot_id(kind, at)):
                    upcoming.append((at, kind))
                    break
        at, kind = min(upcoming)
        return f"{KIND_LABELS[kind]} {format_ts(at.timestamp(), self.tz)}"

    # --- control -----------------------------------------------------------------------------------

    async def tick(self) -> None:
        if self.asleep:
            return
        now = self.clock.now()
        resume = self._resume()
        if resume and not self.brain.lock.locked() and not self.brain.inbox.pending() and now - self._last_activity() >= resume["idle_s"]:
            self._start(resume["kind"], "resume")
            return
        due = self.due_slot(now)
        if due is None:
            return
        kind, slot = due
        if kind == "nap" and self._last_activity() <= self._last_wake():
            # Nothing happened since waking up; there is nothing to nap on.
            self.db.execute(
                "INSERT INTO sleep_runs(kind, reason, slot, started_at, ended_at, status) VALUES ('nap', 'scheduled', ?, ?, ?, 'skipped')",
                (slot, now, now),
            )
            return
        self._start(kind, "scheduled", slot=slot)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("sleep tick failed")
            await asyncio.sleep(TICK_S)

    async def request_sleep(self, reason: str, *, kind: str | None = None, progress: str = "") -> str:
        """Go to sleep now (overflow or manual). Returns what to tell the owner."""
        if self.asleep:
            return "已經在睡了。"
        replaced_kind, replaced = self.replaced_slot(self.clock.now())
        self.progress = progress
        self._start(kind or replaced_kind, reason, replaced=replaced)
        return f"先去{KIND_LABELS[kind or replaced_kind]}，整理完就醒。"

    async def wake(self, reason: str = "owner") -> bool:
        """Interrupted sleep keeps the context; it resumes after a quiet spell."""
        task = self.task
        if task is None or task.done():
            return False
        self._wake_reason = reason
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    def _start(self, kind: str, reason: str, *, slot: str | None = None, replaced: str | None = None) -> None:
        cursor = self.db.execute(
            "INSERT INTO sleep_runs(kind, reason, slot, replaced_slot, session_id, started_at, stage) VALUES (?, ?, ?, ?, ?, ?, 'drowsy')",
            (kind, reason, slot, replaced, self.current_session(), self.clock.now()),
        )
        self.task = asyncio.create_task(self._sleep(cursor.lastrowid, kind, reason))

    # --- one sleep -----------------------------------------------------------------------------------

    def _stage(self, run_id: int, stage: str) -> None:
        self.db.execute("UPDATE sleep_runs SET stage = ? WHERE id = ?", (stage, run_id))

    async def _sleep(self, run_id: int, kind: str, reason: str) -> None:
        stats = RunStats()
        started = self.clock.now()
        try:
            async with self.brain.lock:  # waits for a running turn to finish; new turns wait for us
                self._stage(run_id, "nrem")
                session_id = self.current_session()
                close_session_segments(self.db, session_id, reason="sleep")
                close_idle_segments(self.db, now=self.clock.now())
                if kind == "night":
                    self._backup()
                stats.vectors = await backfill_vectors(self.engine)
                completer = self.completer()
                nrem = Nrem(self.db, self.engine, completer)
                for segment in nrem.pending_segments():
                    if self.clock.now() - started > MAX_DURATION[kind]:
                        break
                    try:
                        result = await nrem.consolidate(segment, run_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("NREM failed on segment %s", segment["id"])
                        stats.failed += 1
                        continue
                    stats.segments += 1
                    stats.events += result.events
                    stats.memories += result.memories
                    stats.reencounters += result.reencounters
                    stats.used += result.used
                    stats.labile += result.labile
                    stats.titles += result.titles
                    stats.new_memories += result.new_memories
                    stats.dropped += result.dropped
                    stats.intentions += result.new_intentions
                day_start = last_night_wake(self.engine)
                if kind == "night":
                    self._stage(run_id, "summaries")
                    stats.summary = await day_summary(self.engine, completer, since=day_start) is not None
                self._stage(run_id, "prune")
                stats.archived = self.engine.prune()
                remaining = self.db.one(
                    "SELECT COUNT(*) AS n FROM segments WHERE session_id = ? AND status <> 'consolidated'", (session_id,)
                )["n"]
                stats.finished = remaining == 0
                self._stage(run_id, "wake")
                if kind == "night":
                    # The dream looks back over the whole day, naps included.
                    day = self.db.query(
                        "SELECT kind, content FROM memories WHERE origin IN ('nrem', 'remember') AND created_at >= ? AND kind <> 'summary' ORDER BY id",
                        (day_start,),
                    )
                    titles = [r["content"].split("：")[0] for r in day if r["kind"] == "event"]
                    learned = [r["content"] for r in day if r["kind"] != "event"]
                    await dream(completer, run_id=run_id, titles=titles, memories=learned, archived=stats.archived, db=self.db, now=self.clock.now())
                self._finish(run_id, "done", stats)
                if stats.finished:
                    self._clear_resume()
                    await self.wake_up(kind, reason, self._wake_line(kind, reason, stats))
                    self.progress = ""
                else:
                    self._set_resume(kind, "unfinished")
                self._safety_net(stats)
        except asyncio.CancelledError:
            self._finish(run_id, "interrupted", stats)
            self._set_resume(kind, self._wake_reason)
            raise
        except Exception:
            log.exception("sleep failed")
            self._finish(run_id, "failed", stats)
            self._set_resume(kind, "unfinished")

    def _finish(self, run_id: int, status: str, stats: RunStats) -> None:
        self.db.execute(
            "UPDATE sleep_runs SET status = ?, ended_at = ?, stats = ? WHERE id = ?",
            (status, self.clock.now(), json.dumps(asdict(stats), ensure_ascii=False), run_id),
        )

    def _wake_line(self, kind: str, reason: str, stats: RunStats) -> str:
        done = f"整理了 {stats.events} 個事件、{stats.memories} 條新記憶，封存 {stats.archived} 條"
        if reason == "overflow":
            line = f"提早睡醒來（context 滿了，{done}）。"
            if self.progress:
                line += f"睡前正在做：{self.progress[:800]}"
            return line
        return f"{KIND_LABELS[kind]}醒來（{done}）。之前的對話已經整理成記憶，context 清空了。"

    def _backup(self) -> None:
        if self.backups_dir is None:
            return
        stamp = datetime.fromtimestamp(self.clock.now(), self.tz)
        self.db.backup(self.backups_dir / f"niya-{stamp:%Y%m%d-%H%M}.db")
        for old in sorted(self.backups_dir.glob("niya-*.db"))[:-BACKUPS_KEPT]:
            old.unlink()

    def _safety_net(self, stats: RunStats) -> None:
        """Plans found in the conversation with no alarm near them: let niya decide whether to set one."""
        lonely = []
        for memory in self.engine.load(stats.intentions).values():
            at = memory.occurred_start
            near = self.db.one(
                "SELECT 1 FROM alarms WHERE status = 'scheduled' AND next_fire_at BETWEEN ? AND ?", (at - 2 * 86400, at)
            )
            if near is None:
                lonely.append(f"m{memory.id}（{format_ts(at, self.tz)}）{memory.content}")
        if lonely:
            self.brain.inbox.put("system", "睡覺整理時發現這些計畫沒有對應的鬧鐘，判斷要不要補設：\n" + "\n".join(f"- {line}" for line in lonely))
