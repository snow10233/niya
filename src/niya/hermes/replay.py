"""Live through imported history again: recall on every owner turn, sleep at every 03:00 and 13:00.

A FakeClock walks forward through the history, so the forgetting curve, the time labels and the
day/week/month summaries come out as if niya had been there. Each waking session starts with the core block
niya would have had; each owner turn gets the automatic recall it would have had, so sleep can judge which
memories were really used. Sleep itself is the real one (sleep.orchestrator.consolidate).

Progress is saved after every sleep: an interrupted replay picks up at the last finished one.
"""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from niya.agent.render import format_ts
from niya.app.runtime import recall_query
from niya.core.clock import FakeClock
from niya.core.db import Database
from niya.hermes.history import SLEEP_HOURS
from niya.memory.engine import MemoryEngine, core_ids
from niya.sleep.llm import Completer
from niya.sleep.orchestrator import KIND_LABELS, RunStats, consolidate, slot_id, spent

PROGRESS_KEY = "replay.at"
EMBED_BATCH = 64
KIND_AT = {3: "night", 13: "nap"}


@dataclass
class ReplayStats:
    turns: int = 0
    recalled: int = 0
    sleeps: int = 0
    skipped_naps: int = 0
    failed_segments: int = 0
    events: int = 0
    memories: int = 0
    used: int = 0
    archived: int = 0
    lines: list[str] = field(default_factory=list)


def slots_between(start: float, end: float, tz: ZoneInfo) -> list[tuple[float, str]]:
    """Every scheduled sleep in (start, end], in order."""
    out = []
    day = datetime.fromtimestamp(start, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() <= end:
        for hour in SLEEP_HOURS:
            at = day.replace(hour=hour).timestamp()
            if start < at <= end:
                out.append((at, KIND_AT[hour]))
        day += timedelta(days=1)
    return out


class Replay:
    def __init__(
        self, db: Database, engine: MemoryEngine, completer: Completer, clock: FakeClock, *, budget_usd: float, parallel: int = 1, report: Callable[[str], None] = print
    ) -> None:
        self.db = db
        self.engine = engine
        self.completer = completer
        self.clock = clock
        self.tz = engine.tz
        self.budget_usd = budget_usd
        self.parallel = parallel
        self.report = report

    def _imported(self, where: str, params=()) -> list:
        return self.db.query(
            f"SELECT t.* FROM turns t JOIN sessions s ON s.id = t.session_id WHERE s.imported_from IS NOT NULL AND {where} ORDER BY t.started_at, t.id",
            params,
        )

    def end_of_history(self) -> float | None:
        """The first night after the last imported turn: by then every day has been slept on."""
        row = self.db.one("SELECT MAX(t.ended_at) AS at FROM turns t JOIN sessions s ON s.id = t.session_id WHERE s.imported_from IS NOT NULL")
        if not row or row["at"] is None:
            return None
        night = [at for at, kind in slots_between(row["at"], row["at"] + 2 * 86400, self.tz) if kind == "night"]
        return night[0]

    async def run(self, *, present: float, until: float | None = None) -> ReplayStats:
        """Replay up to `until` (default: as far as the history goes). Never past `present`: a sleep in the
        future would make the real niya skip it. Whatever is left unslept then goes to its next real sleep."""
        stats = ReplayStats()
        done = float(self.db.get_meta(PROGRESS_KEY) or 0)
        history = self.end_of_history()
        if history is None:
            raise ValueError("沒有匯入的對話可以回放。")
        end = min(history, present, until or history)
        turns = self._imported("t.started_at > ? AND t.started_at <= ?", (done, end))
        if not turns:
            self._hand_over(until, present)
            self.report("已經回放完了。")
            return stats
        # Turns after the last finished sleep were half-done: their recall is redone from scratch.
        marks = ",".join("?" * len(turns))
        self.db.execute(f"DELETE FROM retrieval_events WHERE verdict IS NULL AND turn_id IN ({marks})", [t["id"] for t in turns])
        self.db.execute("UPDATE sleep_runs SET status = 'interrupted' WHERE status = 'running' AND reason = 'replay'")
        pending = list(turns)
        for at, kind in slots_between(max(done, turns[0]["started_at"] - 1), end, self.tz):
            day = []
            while pending and pending[0]["started_at"] < at:
                day.append(pending.pop(0))
            # Memory only changes in sleep, so a whole waking stretch can be embedded in a few calls.
            queries = [self._query(turn) for turn in day]
            vectors = await self._embed([q for q in queries if q])
            for turn, query in zip(day, queries):
                await self._turn(turn, query, vectors.get(query), stats)
            await self._sleep(at, kind, stats)
            self.db.set_meta(PROGRESS_KEY, str(at))
        self._hand_over(until, present)
        return stats

    def _hand_over(self, until: float | None, present: float) -> None:
        if until is None:
            self.db.execute("UPDATE segments SET status = 'closed', closed_reason = 'import' WHERE status = 'replay' AND COALESCE(ended_at, started_at) <= ?", (present,))

    def _query(self, turn) -> str | None:
        """What the live recall hook would have searched with: the owner's words (leaning on the last ones if short)."""
        rows = self.db.query("SELECT content FROM messages WHERE turn_id = ? AND role = 'user' AND speaker = 'owner' AND content IS NOT NULL ORDER BY id", (turn["id"],))
        text = "\n".join(r["content"] for r in rows).strip()
        if not text:
            return None
        previous = self.db.one(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' AND speaker = 'owner' AND content IS NOT NULL AND ts < ? ORDER BY ts DESC, id DESC LIMIT 1",
            (turn["session_id"], turn["started_at"]),
        )
        return recall_query(text, previous["content"] if previous else None)

    async def _embed(self, queries: list[str]) -> dict[str, Any]:
        unique = list(dict.fromkeys(queries))
        out: dict[str, Any] = {}
        for i in range(0, len(unique), EMBED_BATCH):
            batch = unique[i : i + EMBED_BATCH]
            out.update(zip(batch, await self.engine.embed(batch) or ()))  # a failed batch falls back to one call per turn
        return out

    async def _turn(self, turn, query: str | None, vector, stats: ReplayStats) -> None:
        self.clock.set(turn["started_at"])
        stats.turns += 1
        session = self.db.one("SELECT system_prompt FROM sessions WHERE id = ?", (turn["session_id"],))
        if not session["system_prompt"]:
            # What niya would have woken up knowing: the session's core block.
            self.db.execute("UPDATE sessions SET system_prompt = ? WHERE id = ?", (self.engine.core_block() or "（沒有記憶區塊）", turn["session_id"]))
            session = self.db.one("SELECT system_prompt FROM sessions WHERE id = ?", (turn["session_id"],))
        if not query:
            return
        result = await self.engine.recall(query, session_id=turn["session_id"], turn_id=turn["id"], exclude=core_ids(session["system_prompt"]), vector=vector)
        stats.recalled += len(result.ids)

    def _last_wake(self) -> float:
        row = self.db.one("SELECT MAX(ended_at) AS at FROM sleep_runs WHERE status = 'done'")
        return row["at"] if row and row["at"] else 0.0

    async def _sleep(self, at: float, kind: str, stats: ReplayStats) -> None:
        self.clock.set(at)
        slot = slot_id(kind, datetime.fromtimestamp(at, self.tz))
        if self.db.one("SELECT 1 FROM sleep_runs WHERE slot = ? AND status IN ('done', 'skipped')", (slot,)):
            return
        woke = self._last_wake()
        if kind == "nap" and not self._imported("t.started_at > ? AND t.started_at <= ?", (woke, at)):
            # Same rule as the real schedule: nothing happened since waking, nothing to nap on.
            self.db.execute("INSERT INTO sleep_runs(kind, reason, slot, started_at, ended_at, status) VALUES ('nap', 'replay', ?, ?, ?, 'skipped')", (slot, at, at))
            stats.skipped_naps += 1
            return
        # The day's segments are over; hand them to sleep.
        self.db.execute("UPDATE segments SET status = 'closed', closed_reason = 'import' WHERE status = 'replay' AND COALESCE(ended_at, started_at) <= ?", (at,))
        run_id = self.db.execute(
            "INSERT INTO sleep_runs(kind, reason, slot, started_at, stage) VALUES (?, 'replay', ?, ?, 'nrem')", (kind, slot, at)
        ).lastrowid
        run = RunStats()
        try:
            await consolidate(self.db, self.engine, self.completer, run_id=run_id, kind=kind, stats=run, started=at, budget_usd=self.budget_usd, parallel=self.parallel)
        except BaseException:
            self.db.execute("UPDATE sleep_runs SET status = 'interrupted', stats = ? WHERE id = ?", (json.dumps(asdict(run), ensure_ascii=False), run_id))
            raise
        run.finished = True
        self.db.execute(
            "UPDATE sleep_runs SET status = 'done', ended_at = ?, stats = ?, cost_usd = ? WHERE id = ?",
            (self.clock.now(), json.dumps(asdict(run), ensure_ascii=False), spent(self.db, at), run_id),
        )
        stats.sleeps += 1
        stats.failed_segments += run.failed
        stats.events += run.events
        stats.memories += run.memories
        stats.used += run.used
        stats.archived += run.archived
        line = (
            f"{format_ts(at, self.tz)} {KIND_LABELS[kind]}：{run.segments} 段 → {run.events} 個事件、{run.memories} 條記憶，"
            f"{run.used} 條用上變牢，封存 {run.archived} 條" + (f"，{run.failed} 段失敗" if run.failed else "")
        )
        stats.lines.append(line)
        self.report(line)
