"""NREM: turn each closed segment of the raw record into events and long-term memories.

One LLM call per segment (no tools, JSON out) cuts it into events, extracts facts, preferences and plans,
and says which surfaced memories were really used. Code validates the cut (falling back to one event per
segment), deduplicates against existing memories, and writes everything for a segment in one
transaction, so a crash or an interrupted sleep never leaves half a segment behind.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from niya.agent.render import SPEAKER_LABELS, format_ts
from niya.db import Database
from niya.memory.engine import LIVE, Memory, MemoryEngine
from niya.memory.vectors import store_vector
from niya.sleep.llm import Completer, parse_json
from niya.sleep.prompts import NREM_SYSTEM
from niya.tools.threat_patterns import first_threat_message

log = logging.getLogger(__name__)

DUPLICATE_COS = 0.92
SIMILAR_COS = 0.75
LINE_CHARS = 800
SHORT_LINE_CHARS = 300
CHUNK_CHARS = 30_000
KINDS = ("fact", "preference", "intention")
_WHEN = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?\s*$")


@dataclass
class SegmentResult:
    events: int = 0
    memories: int = 0
    reencounters: int = 0
    used: int = 0
    labile: int = 0
    fallback: bool = False
    titles: list[str] = field(default_factory=list)
    new_memories: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    new_intentions: list[int] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)


@dataclass
class _Turn:
    label: str
    id: str
    started_at: float
    ended_at: float
    gap_s: float | None
    sim_prev: float | None
    rows: list[Any]


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _parse_when(text: Any, tz: ZoneInfo) -> tuple[float | None, str | None]:
    if not isinstance(text, str) or not (match := _WHEN.match(text)):
        return None, None
    year, month, day, hour, minute = match.groups()
    try:
        moment = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), tzinfo=tz)
    except ValueError:
        return None, None
    return moment.timestamp(), "minute" if hour is not None else "day"


def _int(value: Any, default: int, low: int = 1, high: int = 10) -> int:
    try:
        return min(max(int(value), low), high)
    except (TypeError, ValueError):
        return default


class Nrem:
    def __init__(self, db: Database, engine: MemoryEngine, completer: Completer) -> None:
        self.db = db
        self.engine = engine
        self.completer = completer
        self.tz = engine.tz

    def pending_segments(self) -> list[Any]:
        return self.db.query("SELECT * FROM segments WHERE status = 'closed' ORDER BY started_at, id")

    # --- input ---------------------------------------------------------------------------------------

    def _turns(self, segment_id: int) -> list[_Turn]:
        turns = self.db.query("SELECT * FROM turns WHERE segment_id = ? ORDER BY started_at, id", (segment_id,))
        out = []
        for i, t in enumerate(turns, start=1):
            rows = self.db.query("SELECT * FROM messages WHERE turn_id = ? ORDER BY id", (t["id"],))
            out.append(_Turn(f"t{i}", t["id"], t["started_at"], t["ended_at"] or t["started_at"], t["gap_s"], t["sim_prev"], rows))
        return out

    def _transcript(self, turns: list[_Turn], limit: int) -> str:
        blocks = []
        for turn in turns:
            notes = []
            if turn.gap_s is not None:
                notes.append(f"距上一回合 {int(turn.gap_s // 60)} 分鐘")
            if turn.sim_prev is not None:
                notes.append(f"主題相似度 {turn.sim_prev:.2f}")
            lines = [f"[{turn.label}] {format_ts(turn.started_at, self.tz)}" + (f"（{'，'.join(notes)}）" if notes else "")]
            for row in turn.rows:
                if row["role"] == "user":
                    lines.append(f"  {SPEAKER_LABELS.get(row['speaker'] or 'owner', 'user')}: {_clip(row['content'], limit)}")
                elif row["role"] == "assistant":
                    if row["content"]:
                        lines.append(f"  niya: {_clip(row['content'], limit)}")
                    calls = json.loads(row["tool_calls"]) if row["tool_calls"] else []
                    if calls:
                        names = [f"{c['function']['name']}({_clip(c['function']['arguments'], 100)})" for c in calls[:5]]
                        lines.append("  niya 用了工具：" + "、".join(names))
                elif row["role"] == "tool" and (row["content"] or "").startswith(("錯誤", "⛔")):
                    lines.append(f"  （{row['tool_name']} 失敗：{_clip(row['content'], 120)}）")
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    def _chunks(self, turns: list[_Turn]) -> list[list[_Turn]]:
        """Very long segments are consolidated in pieces that fit comfortably in one call."""
        chunks: list[list[_Turn]] = []
        current: list[_Turn] = []
        size = 0
        for turn in turns:
            cost = len(self._transcript([turn], SHORT_LINE_CHARS))
            if current and size + cost > CHUNK_CHARS:
                chunks.append(current)
                current, size = [], 0
            current.append(turn)
            size += cost
        if current:
            chunks.append(current)
        return chunks

    async def _references(self, segment: Any, turns: list[_Turn]) -> tuple[str, dict[int, Memory], dict[int, Memory], list[Memory], list[Any]]:
        now = self.engine.clock.now()
        recent = self.db.query(
            "SELECT * FROM memories WHERE kind = 'event' AND origin = 'nrem' AND state = 'active' AND occurred_start >= ? ORDER BY occurred_start DESC LIMIT 15",
            (segment["started_at"] - 7 * 86400,),
        )
        recent_events = {r["id"]: self.engine.get(r["id"]) for r in recent}
        owner_text = "\n".join(row["content"] for t in turns for row in t.rows if row["role"] == "user" and row["speaker"] == "owner" and row["content"])[:2000]
        related: dict[int, Memory] = {}
        if owner_text:
            vectors = await self.engine.embed([owner_text])
            if vectors is not None:
                related = {m.id: m for m, score in self.engine.nearest(vectors[0], 12) if m.kind in KINDS and score >= 0.3}
        turn_ids = [t.id for t in turns]
        marks = ",".join("?" * len(turn_ids))
        retrievals = self.db.query(f"SELECT * FROM retrieval_events WHERE turn_id IN ({marks}) AND verdict IS NULL", turn_ids) if turn_ids else []
        surfaced = self.engine.load(r["memory_id"] for r in retrievals)
        labile = [
            m
            for m in self.engine.load(
                r["id"]
                for r in self.db.query(
                    "SELECT id FROM memories WHERE state = 'labile' AND source_session_id = ? AND created_at >= ? AND created_at <= ?",
                    (segment["session_id"], segment["started_at"] - 60, (segment["ended_at"] or now) + 60),
                )
            ).values()
        ]
        lines = []
        if recent_events:
            lines += ["## 最近的事件（接續的話用 continues 填編號）"] + [f"- {i} [{format_ts(m.occurred_start, self.tz)}] {_clip(m.content, 80)}" for i, m in recent_events.items() if m]
        if related:
            lines += ["## 可能相關的既有記憶（新資訊推翻舊的時，用 replaces 填編號）"] + [f"- {i} {m.content}" for i, m in related.items()]
        if surfaced:
            lines += ["## 這段對話中浮現過的記憶（真的用上的編號放進 used_memories）"] + [f"- {i} {m.content}" for i, m in surfaced.items()]
        if labile:
            lines += ["## 白天刻意記住的（已經記下，不要重複萃取）"] + [f"- {m.content}" for m in labile]
        # Either list may hold the fact that new information overturns.
        replaceable = {**related, **{i: m for i, m in surfaced.items() if m.kind in KINDS}}
        return "\n".join(lines), recent_events, replaceable, labile, retrievals

    # --- one segment -------------------------------------------------------------------------------

    async def _ask(self, prompt: str) -> dict[str, Any] | None:
        for attempt in range(2):
            try:
                reply = await self.completer.complete(system=NREM_SYSTEM, prompt=prompt, task="nrem")
                data = parse_json(reply)
                if isinstance(data, dict):
                    return data
            except ValueError as exc:
                log.warning("NREM reply was not usable (attempt %d): %s", attempt + 1, exc)
        return None

    def _validate(self, data: dict[str, Any] | None, turns: list[_Turn]) -> tuple[list[dict[str, Any]], bool]:
        labels = [t.label for t in turns]
        events = data.get("events") if data else None
        if isinstance(events, list) and events:
            flat = [label for e in events if isinstance(e, dict) for label in (e.get("turns") or [])]
            if flat == labels and all(isinstance(e, dict) and e.get("turns") for e in events):
                return events, False
        # Fallback: the whole chunk is one event; keep whatever memories were extracted.
        owner = next((row["content"] for t in turns for row in t.rows if row["role"] == "user" and row["content"]), "對話")
        memories = [m for e in (events or []) if isinstance(e, dict) for m in (e.get("memories") or [])]
        return [{"turns": labels, "title": _clip(owner, 40), "summary": "", "outcome": "", "importance": 3, "trivial": False, "memories": memories}], True

    async def consolidate(self, segment: Any, run_id: int) -> SegmentResult:
        result = SegmentResult()
        turns = self._turns(segment["id"])
        if not turns:
            self.db.execute("UPDATE segments SET status = 'consolidated', consolidated_at = ?, sleep_run_id = ? WHERE id = ?", (self.engine.clock.now(), run_id, segment["id"]))
            return result
        references, recent_events, related, labile, retrievals = await self._references(segment, turns)
        plans = []
        used: set[int] = set()
        chunks = self._chunks(turns)
        limit = LINE_CHARS if len(chunks) == 1 else SHORT_LINE_CHARS
        for chunk in chunks:
            prompt = f"現在是 {format_ts(self.engine.clock.now(), self.tz)}。以下是一段對話（片段 #{segment['id']}）。\n\n## 對話\n{self._transcript(chunk, limit)}\n\n{references}"
            data = await self._ask(prompt)
            events, fallback = self._validate(data, chunk)
            result.fallback |= fallback
            used |= {int(i) for i in (data or {}).get("used_memories") or [] if str(i).isdigit()}
            plans.append((chunk, events))

        texts: list[str] = []
        for chunk, events in plans:
            for event in events:
                if not event.get("trivial"):
                    texts.append(self._event_text(event))
                for memory in event.get("memories") or []:
                    if isinstance(memory, dict) and memory.get("content"):
                        texts.append(str(memory["content"]))
        vectors = await self.engine.embed(texts) if texts else None
        vector_iter = iter(vectors or [])

        try:
            with self.db.transaction():
                for chunk, events in plans:
                    by_label = {t.label: t for t in chunk}
                    for event in events:
                        self._write_event(segment, event, by_label, recent_events, related, vectors is not None, vector_iter, result)
                for memory in labile:
                    self.engine.consolidate_labile(memory)
                    if result.event_ids:
                        self.engine.add_link(memory.id, result.event_ids[-1], "derived_from")
                    result.labile += 1
                now = self.engine.clock.now()
                for event in retrievals:
                    verdict = "used" if event["memory_id"] in used else "unused"
                    self.db.execute("UPDATE retrieval_events SET verdict = ?, judged_at = ? WHERE id = ?", (verdict, now, event["id"]))
                    memory = self.engine.get(event["memory_id"])
                    if verdict == "used" and memory is not None and self.engine.review(memory, kind="use", ref_id=event["turn_id"] or str(event["id"]), at=event["ts"], r=event["r_at"]):
                        result.used += 1
                self.db.execute("UPDATE segments SET status = 'consolidated', consolidated_at = ?, sleep_run_id = ? WHERE id = ?", (now, run_id, segment["id"]))
        except BaseException:
            self.engine.reload_index()
            raise
        return result

    @staticmethod
    def _event_text(event: dict[str, Any]) -> str:
        text = f"{_clip(str(event.get('title') or '對話'), 80)}"
        if event.get("summary"):
            text += f"：{_clip(str(event['summary']), 600)}"
        if event.get("outcome"):
            text += f"（{_clip(str(event['outcome']), 40)}）"
        return text

    def _write_event(self, segment, event, by_label, recent_events, related, have_vectors, vector_iter, result: SegmentResult) -> None:
        turns = [by_label[label] for label in event["turns"] if label in by_label]
        start, end = turns[0].started_at, max(t.ended_at for t in turns)
        event_memory: Memory | None = None
        if not event.get("trivial"):
            vector = next(vector_iter) if have_vectors else None
            event_memory = self.engine.create(
                kind="event",
                content=self._event_text(event),
                importance=_int(event.get("importance"), 3),
                occurred_start=start,
                occurred_end=end,
                precision="minute",
                origin="nrem",
                session_id=segment["session_id"],
                vector=vector,
            )
            self.engine.add_sources(event_memory.id, (row["id"] for t in turns for row in t.rows))
            previous = event.get("continues")
            if isinstance(previous, int) and previous in recent_events:
                self.engine.add_link(event_memory.id, previous, "continues")
            result.events += 1
            result.event_ids.append(event_memory.id)
            result.titles.append(_clip(str(event.get("title") or ""), 60))
        for candidate in event.get("memories") or []:
            if not isinstance(candidate, dict) or not candidate.get("content"):
                continue
            vector = next(vector_iter) if have_vectors else None
            content = _clip(str(candidate["content"]), 600)
            if threat := first_threat_message(content, "strict"):
                result.dropped.append(f"{content}（{threat}）")
                continue
            self._write_candidate(segment, candidate, content, vector, start, end, event_memory, related, result)

    def _write_candidate(self, segment, candidate, content, vector, start, end, event_memory, related, result: SegmentResult) -> None:
        kind = candidate.get("kind") if candidate.get("kind") in KINDS else "fact"
        if vector is not None:
            nearest = self.engine.nearest(vector, 1)
            if nearest and nearest[0][1] >= DUPLICATE_COS and nearest[0][0].state in LIVE:
                existing = nearest[0][0]
                # Meeting something again in real life strengthens it; it is not a new memory.
                self.engine.review(existing, kind="reencounter", ref_id=f"segment-{segment['id']}", at=end)
                if event_memory:
                    self.engine.add_link(existing.id, event_memory.id, "derived_from")
                result.reencounters += 1
                return
        when, precision = _parse_when(candidate.get("when"), self.tz)
        source = candidate.get("source") if candidate.get("source") in ("owner", "self", "external") else "self"
        memory = self.engine.create(
            kind=kind,
            content=content,
            importance=_int(candidate.get("importance"), 4),
            occurred_start=when if when is not None else (None if kind == "intention" else start),
            occurred_end=when if when is not None else (None if kind == "intention" else end),
            precision=precision or ("unknown" if kind == "intention" else "minute"),
            origin="nrem",
            trust=source,
            session_id=segment["session_id"],
            vector=vector,
        )
        if event_memory:
            self.engine.add_link(memory.id, event_memory.id, "derived_from")
        replaced = candidate.get("replaces")
        if isinstance(replaced, int) and replaced in related and related[replaced].state in LIVE:
            self.engine.supersede(related[replaced], memory, start)
        elif vector is not None:
            for other, score in self.engine.nearest(vector, 3):
                if other.id != memory.id and score >= SIMILAR_COS:
                    self.engine.add_link(memory.id, other.id, "similar", score)
        if kind == "intention" and when is not None and when > self.engine.clock.now():
            result.new_intentions.append(memory.id)
        result.memories += 1
        result.new_memories.append(content)


async def backfill_vectors(engine: MemoryEngine, batch: int = 64) -> int:
    """Memories written while embeddings were down get their vectors now."""
    if engine.embedder is None:
        return 0
    rows = engine.db.query(
        "SELECT m.id, m.content FROM memories m WHERE m.state IN ('labile', 'active', 'archived')"
        " AND NOT EXISTS (SELECT 1 FROM memory_vectors v WHERE v.memory_id = m.id AND v.model = ?) ORDER BY m.id",
        (engine.embedder.model,),
    )
    done = 0
    for i in range(0, len(rows), batch):
        part = rows[i : i + batch]
        vectors = await engine.embed([r["content"] for r in part])
        if vectors is None:
            break
        for row, vector in zip(part, vectors):
            stored = store_vector(engine.db, row["id"], engine.embedder.model, vector)
            state = engine.db.one("SELECT state FROM memories WHERE id = ?", (row["id"],))["state"]
            if state in LIVE:
                engine.index.add(row["id"], np.asarray(stored))
            done += 1
    return done
