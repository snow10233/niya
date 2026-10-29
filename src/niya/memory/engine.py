"""The memory engine: encoding, automatic recall each turn, deliberate recall, and the core block.

Retrieval is done by code (embeddings, full-text search, time cues and the forgetting curve), never by
letting the model read every memory. Surfacing a memory only counts as exposure; sleep later decides
which surfaced memories were really used and strengthens those.
"""

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from niya.clock import Clock
from niya.db import Database
from niya.memory.curve import DAY, DEFAULT, CurveParams, cue_threshold, initial_stability, r_at, recall_score, reinforce
from niya.memory.search import match_expression, query_terms, search_messages
from niya.memory.timefmt import exact_label, time_label
from niya.memory.timeparse import parse_time_range
from niya.memory.vectors import Embedder, VectorIndex, normalise, search_archived, store_vector

log = logging.getLogger(__name__)

KINDS = ("fact", "preference", "intention", "event", "insight", "summary")
KIND_LABELS = {"event": "事件", "fact": "事實", "preference": "偏好", "intention": "計畫", "insight": "想法", "summary": "摘要"}
TRUSTS = ("owner", "self", "external")
LIVE = ("labile", "active")
MAX_CONTENT = 600
DUPLICATE_COS = 0.95
AUTO_MAX_ITEMS = 6
AUTO_MAX_CHARS = 900  # about 600 tokens of Chinese
DEEP_MEMORIES = 8
DEEP_MESSAGES = 6
CORE_MAX_CHARS = 1800  # about 1.2k tokens
FADED_R = 0.5
# Text matches are a small tie-breaker next to embeddings, but must carry recall alone when embeddings are down.
FTS_WEIGHT = 0.1
FTS_ONLY_WEIGHT = 0.45
_CORE_REF = re.compile(r"^- m(\d+) ", re.M)


@dataclass(frozen=True)
class Memory:
    id: int
    kind: str
    level: str | None
    content: str
    importance: int
    state: str
    stability: float
    last_review_at: float
    occurred_start: float | None
    occurred_end: float | None
    time_precision: str
    valid_from: float | None
    valid_until: float | None
    origin: str
    trust: str
    source_session_id: str | None
    created_at: float


@dataclass(frozen=True)
class Candidate:
    memory: Memory
    r: float
    rel: float
    score: float


@dataclass(frozen=True)
class RecallResult:
    block: str | None
    ids: list[int]
    tip: int


def _memory(row) -> Memory:
    return Memory(**{name: row[name] for name in Memory.__dataclass_fields__})


def core_ids(system_prompt: str) -> set[int]:
    """Memories already shown in this session's core block (lines start with their ref)."""
    return {int(i) for i in _CORE_REF.findall(system_prompt)}


class MemoryEngine:
    def __init__(self, db: Database, clock: Clock, tz: ZoneInfo, *, embedder: Embedder | None = None, params: CurveParams = DEFAULT) -> None:
        self.db = db
        self.clock = clock
        self.tz = tz
        self.embedder = embedder
        self.params = params
        self.index = VectorIndex.load(db, embedder.model) if embedder else VectorIndex()
        self._last_query: dict[str, np.ndarray] = {}

    # --- storage -----------------------------------------------------------------------------------

    def load(self, ids: Iterable[int]) -> dict[int, Memory]:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return {r["id"]: _memory(r) for r in self.db.query(f"SELECT * FROM memories WHERE id IN ({marks})", ids)}

    def get(self, memory_id: int) -> Memory | None:
        return self.load([memory_id]).get(memory_id)

    def r(self, memory: Memory, now: float | None = None) -> float:
        return r_at(self.clock.now() if now is None else now, memory.last_review_at, memory.stability)

    async def embed(self, texts: Sequence[str]) -> list[np.ndarray] | None:
        if self.embedder is None or not texts:
            return None
        try:
            return [normalise(v) for v in await self.embedder.embed(list(texts))]
        except Exception as exc:  # recall degrades to full-text search; vectors are filled in at sleep
            log.warning("embedding failed: %s", exc)
            return None

    def log_retrievals(self, memories: Iterable[tuple[Memory, float]], *, channel: str, session_id: str | None, turn_id: str | None) -> None:
        now = self.clock.now()
        for memory, r in memories:
            self.db.execute(
                "INSERT INTO retrieval_events(memory_id, channel, session_id, turn_id, ts, r_at) VALUES (?, ?, ?, ?, ?, ?)",
                (memory.id, channel, session_id, turn_id, now, r),
            )

    # --- encoding ----------------------------------------------------------------------------------

    async def remember(
        self,
        content: str,
        *,
        kind: str = "fact",
        importance: int = 5,
        trust: str = "self",
        occurred_start: float | None = None,
        occurred_end: float | None = None,
        precision: str | None = None,
        session_id: str | None = None,
        message_ids: Sequence[int] = (),
    ) -> tuple[Memory, bool]:
        """A deliberate daytime memory: labile (fragile) until sleep consolidates it. Returns (memory, is_new)."""
        content = re.sub(r"\s+", " ", content).strip()
        if not content:
            raise ValueError("要記住的內容不能是空的。")
        if len(content) > MAX_CONTENT:
            raise ValueError(f"一條記憶最多 {MAX_CONTENT} 字，請拆開或精簡。")
        if kind not in KINDS:
            raise ValueError(f"kind 只能是 {'、'.join(KINDS)}。")
        if trust not in TRUSTS:
            raise ValueError(f"trust 只能是 {'、'.join(TRUSTS)}。")
        importance = min(max(int(importance), 1), 10)
        if trust == "external":
            importance = min(importance, 6)
        vectors = await self.embed([content])
        if vectors is not None and (top := self.index.search(vectors[0], 1)) and top[0][1] >= DUPLICATE_COS:
            existing = self.get(top[0][0])
            if existing is not None:
                return existing, False
        now = self.clock.now()
        if occurred_start is None and kind != "intention":
            occurred_start, precision = now, precision or "minute"
        cursor = self.db.execute(
            "INSERT INTO memories(kind, content, importance, state, stability, last_review_at, occurred_start, occurred_end,"
            " time_precision, origin, trust, source_session_id, created_at) VALUES (?, ?, ?, 'labile', ?, ?, ?, ?, ?, 'remember', ?, ?, ?)",
            (
                kind,
                content,
                importance,
                initial_stability(importance, self.params) * self.params.labile_factor,
                now,
                occurred_start,
                occurred_end if occurred_end is not None else occurred_start,
                precision or ("unknown" if occurred_start is None else "minute"),
                trust,
                session_id,
                now,
            ),
        )
        memory_id = cursor.lastrowid
        for message_id in message_ids:
            self.db.execute("INSERT OR IGNORE INTO memory_sources(memory_id, message_id) VALUES (?, ?)", (memory_id, message_id))
        if vectors is not None:
            self.index.add(memory_id, store_vector(self.db, memory_id, self.embedder.model, vectors[0]))
        return self.get(memory_id), True

    # --- consolidation (used by sleep) -------------------------------------------------------------

    def reload_index(self) -> None:
        """After a rolled-back consolidation the in-memory index may hold rows that never got committed."""
        self.index = VectorIndex.load(self.db, self.embedder.model) if self.embedder else VectorIndex()

    def create(
        self,
        *,
        kind: str,
        content: str,
        importance: int,
        occurred_start: float | None,
        occurred_end: float | None,
        precision: str,
        origin: str,
        trust: str = "self",
        state: str = "active",
        stability: float | None = None,
        level: str | None = None,
        valid_from: float | None = None,
        session_id: str | None = None,
        vector: np.ndarray | None = None,
    ) -> Memory:
        importance = min(max(int(importance), 1), 10)
        if trust == "external":
            importance = min(importance, 6)
        if stability is None:
            stability = initial_stability(importance, self.params) * (self.params.level_mult.get(level or "", 1.0))
        encoded = occurred_end if occurred_end is not None else occurred_start
        cursor = self.db.execute(
            "INSERT INTO memories(kind, level, content, importance, state, stability, last_review_at, occurred_start, occurred_end,"
            " time_precision, valid_from, origin, trust, source_session_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind, level, content, importance, state, stability,
                # The clock starts when it happened, not when sleep wrote it down; plans start from now.
                min(encoded, self.clock.now()) if encoded is not None else self.clock.now(),
                occurred_start, occurred_end, precision, valid_from, origin, trust, session_id, self.clock.now(),
            ),
        )
        memory_id = cursor.lastrowid
        if vector is not None and self.embedder is not None:
            self.index.add(memory_id, store_vector(self.db, memory_id, self.embedder.model, vector))
        return self.get(memory_id)

    def add_link(self, src_id: int, dst_id: int, relation: str, weight: float | None = None) -> None:
        if src_id != dst_id:
            self.db.execute(
                "INSERT OR IGNORE INTO memory_links(src_id, dst_id, relation, weight, created_at) VALUES (?, ?, ?, ?, ?)",
                (src_id, dst_id, relation, weight, self.clock.now()),
            )

    def add_sources(self, memory_id: int, message_ids: Iterable[int]) -> None:
        for message_id in message_ids:
            self.db.execute("INSERT OR IGNORE INTO memory_sources(memory_id, message_id) VALUES (?, ?)", (memory_id, message_id))

    def nearest(self, vector: np.ndarray, k: int = 5) -> list[tuple[Memory, float]]:
        hits = self.index.search(vector, k)
        found = self.load(i for i, _ in hits)
        return [(found[i], score) for i, score in hits if i in found]

    def review(self, memory: Memory, *, kind: str, ref_id: str, at: float, r: float | None = None) -> bool:
        """Strengthen a memory that was really used (or met again) at `at`. Idempotent per (memory, kind, ref)."""
        r = self.r(memory, at) if r is None else r
        weight_kind = "relearn" if memory.state == "archived" and kind == "use" else kind
        new_s = reinforce(memory.stability, r, weight_kind, self.params)
        inserted = self.db.execute(
            "INSERT OR IGNORE INTO memory_reviews(memory_id, kind, ref_id, ts, r_before, s_before, s_after) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (memory.id, kind, ref_id, at, r, memory.stability, new_s),
        ).rowcount
        if not inserted:
            return False
        state = "active" if memory.state == "archived" else memory.state
        self.db.execute(
            "UPDATE memories SET stability = ?, last_review_at = MAX(last_review_at, ?), review_count = review_count + 1, state = ? WHERE id = ?",
            (new_s, at, state, memory.id),
        )
        if memory.state == "archived" and self.embedder is not None:
            row = self.db.one("SELECT dim, vec FROM memory_vectors WHERE memory_id = ? AND model = ?", (memory.id, self.embedder.model))
            if row:
                self.index.add(memory.id, np.frombuffer(row["vec"], dtype="<f4").copy())
        return True

    def consolidate_labile(self, memory: Memory) -> None:
        """A memory from the day survives its first night: it gets the full stability its importance earns."""
        self.db.execute(
            "UPDATE memories SET state = 'active', stability = MAX(stability, ?) WHERE id = ? AND state = 'labile'",
            (initial_stability(memory.importance, self.params), memory.id),
        )

    def supersede(self, old: Memory, new: Memory, at: float) -> None:
        """New information replaces an old fact; both keep their validity period for 'it used to be…' questions."""
        self.db.execute(
            "UPDATE memories SET state = 'superseded', superseded_by = ?, valid_until = ? WHERE id = ?",
            (new.id, at, old.id),
        )
        self.db.execute("UPDATE memories SET valid_from = COALESCE(valid_from, ?) WHERE id = ?", (at, new.id))
        self.add_link(new.id, old.id, "supersedes")
        self.index.remove(old.id)

    def prune(self) -> int:
        """Archive what has faded below the line; returns how many."""
        now = self.clock.now()
        archived = 0
        for row in self.db.query("SELECT * FROM memories WHERE state IN ('labile', 'active')"):
            memory = _memory(row)
            r = self.r(memory, now)
            if r < self.params.archive_r:
                self.db.execute("UPDATE memories SET state = 'archived', r_cached = ?, lapse_count = lapse_count + 1 WHERE id = ?", (r, memory.id))
                self.index.remove(memory.id)
                archived += 1
            else:
                self.db.execute("UPDATE memories SET r_cached = ? WHERE id = ?", (r, memory.id))
        return archived

    # --- candidate search ----------------------------------------------------------------------------

    def _fts(self, query: str, states: Sequence[str], k: int = 20) -> dict[int, float]:
        """Full-text hits as a 0..1 rank bonus (best hit 1.0)."""
        terms, fallback = query_terms(query)
        marks = ",".join("?" * len(states))
        rows: list = []
        if expression := match_expression(terms):
            rows = self.db.query(
                f"SELECT m.id FROM memories_tri JOIN memories m ON m.id = memories_tri.rowid WHERE memories_tri MATCH ? AND m.state IN ({marks})"
                " ORDER BY bm25(memories_tri) LIMIT ?",
                (expression, *states, k),
            )
        if not rows and fallback:
            like = " OR ".join("content LIKE ?" for _ in fallback)
            found = self.db.query(f"SELECT id, content FROM memories WHERE state IN ({marks}) AND ({like}) LIMIT 200", (*states, *(f"%{t}%" for t in fallback)))
            ranked = sorted(found, key=lambda r: -sum(t in r["content"].lower() for t in fallback))[:k]
            rows = ranked
        return {r["id"]: 1.0 - i / max(len(rows), 1) for i, r in enumerate(rows)}

    def _in_range(self, since: float, until: float, states: Sequence[str]) -> set[int]:
        marks = ",".join("?" * len(states))
        rows = self.db.query(
            f"SELECT id FROM memories WHERE state IN ({marks}) AND occurred_start IS NOT NULL"
            " AND occurred_start < ? AND COALESCE(occurred_end, occurred_start) >= ?",
            (*states, until, since),
        )
        return {r["id"] for r in rows}

    def _shown_in_session(self, session_id: str) -> set[int]:
        rows = self.db.query("SELECT DISTINCT memory_id FROM retrieval_events WHERE session_id = ? AND channel = 'auto'", (session_id,))
        return {r["memory_id"] for r in rows}

    # --- automatic recall --------------------------------------------------------------------------

    async def recall(self, query: str, *, session_id: str, turn_id: str, exclude: Iterable[int] = ()) -> RecallResult:
        """What comes to mind on its own for this turn; also measures how much the topic moved (sim_prev)."""
        now = self.clock.now()
        vectors = await self.embed([query])
        cos: dict[int, float] = {}
        if vectors is not None:
            vec = vectors[0]
            cos = dict(self.index.search(vec, 50))
            if (previous := self._last_query.get(session_id)) is not None:
                self.db.execute("UPDATE turns SET sim_prev = ? WHERE id = ?", (float(previous @ vec), turn_id))
            self._last_query[session_id] = vec
        fts = self._fts(query, LIVE)
        fts_weight = FTS_WEIGHT if vectors is not None else FTS_ONLY_WEIGHT
        span = parse_time_range(query, now, self.tz)
        timed = self._in_range(*span, LIVE) if span else set()
        skip = set(exclude) | self._shown_in_session(session_id)
        picked: list[Candidate] = []
        tip = 0
        for memory in self.load([*cos, *fts, *timed]).values():
            if memory.state not in LIVE or memory.id in skip or memory.source_session_id == session_id:
                continue
            r = self.r(memory, now)
            rel = cos.get(memory.id, 0.0) + fts_weight * fts.get(memory.id, 0.0) + (0.3 if memory.id in timed else 0.0)
            if rel < cue_threshold(r, self.params):
                tip += rel >= self.params.tip_rel
                continue
            picked.append(Candidate(memory, r, rel, recall_score(rel, r, memory.importance)))
        tip += min(len(self._fts(query, ("archived",), k=3)), 3)
        chosen: list[Candidate] = []
        size = 0
        for candidate in sorted(picked, key=lambda c: -c.score):
            line = self._auto_line(candidate, now)
            if len(chosen) >= AUTO_MAX_ITEMS or size + len(line) > AUTO_MAX_CHARS:
                break
            chosen.append(candidate)
            size += len(line)
        self.log_retrievals(((c.memory, c.r) for c in chosen), channel="auto", session_id=session_id, turn_id=turn_id)
        if not chosen and not tip:
            return RecallResult(None, [], 0)
        lines = ["<recalled_memories>", *(self._auto_line(c, now) for c in chosen)]
        if tip:
            lines.append(f"（還有 {tip} 條可能相關、但想不太起來的記憶，需要時用 deep_recall）")
        lines.append("</recalled_memories>")
        return RecallResult("\n".join(lines), [c.memory.id for c in chosen], tip)

    def _auto_line(self, candidate: Candidate, now: float) -> str:
        m = candidate.memory
        tags = [time_label(m.occurred_start, m.time_precision, candidate.r, now, self.tz), KIND_LABELS[m.kind]]
        if candidate.r < FADED_R:
            tags.append("印象模糊")
        return f"- m{m.id} [{'・'.join(tags)}] {m.content}"

    # --- deliberate recall -------------------------------------------------------------------------

    def _exact_line(self, memory: Memory, now: float) -> str:
        tags = [exact_label(memory.occurred_start, memory.occurred_end, self.tz) if memory.occurred_start else "時間不明", KIND_LABELS[memory.kind]]
        if memory.state == "archived":
            tags.append("要很用力才想起來")
        elif memory.state == "labile":
            tags.append("剛記下、還不牢")
        if memory.valid_until is not None:
            tags.append(f"到 {datetime.fromtimestamp(memory.valid_until, self.tz):%Y-%m-%d} 為止")
        return f"- m{memory.id} [{'・'.join(tags)}] {memory.content}"

    async def deep_recall(self, query: str, *, session_id: str | None, turn_id: str | None, since: float | None = None, until: float | None = None) -> str:
        """Trying hard to remember: archived memories included, no cue threshold, exact times."""
        now = self.clock.now()
        states = ("labile", "active", "archived")
        vectors = await self.embed([query])
        cos: dict[int, float] = {}
        if vectors is not None:
            cos = dict(self.index.search(vectors[0], 30))
            cos.update(search_archived(self.db, self.embedder.model, vectors[0], 10))
        fts = self._fts(query, states)
        span = (since, until) if since is not None or until is not None else parse_time_range(query, now, self.tz)
        timed = self._in_range(span[0] or float("-inf"), span[1] or float("inf"), states) if span else set()
        scored = []
        for memory in self.load([*cos, *fts, *timed]).values():
            if memory.state not in states:
                continue
            if span and memory.id not in timed and (since is not None or until is not None):
                continue
            rel = cos.get(memory.id, 0.0) + (FTS_WEIGHT if vectors is not None else FTS_ONLY_WEIGHT) * fts.get(memory.id, 0.0) + (0.3 if memory.id in timed else 0.0)
            if rel >= self.params.cue_base or memory.id in fts:
                scored.append((rel, memory))
        scored.sort(key=lambda item: -item[0])
        memories = [m for _, m in scored[:DEEP_MEMORIES]]
        self.log_retrievals(((m, self.r(m, now)) for m in memories), channel="deep", session_id=session_id, turn_id=turn_id)
        hits = search_messages(self.db, query, since=since, until=until, exclude_session=session_id, limit=DEEP_MESSAGES)
        if not memories and not hits:
            return f"怎麼想都想不起「{query}」。可以換個關鍵字，或放寬時間範圍。"
        lines: list[str] = []
        if memories:
            lines += ["記憶：", *(self._exact_line(m, now) for m in memories)]
        if hits:
            lines += ["以前的對話（都是先前的 session，不在目前的 context 裡）："]
            lines += [f"- msg#{h.id} [{exact_label(h.ts, None, self.tz)}] {'niya' if h.role == 'assistant' else _speaker(h.speaker)}: {h.snippet}" for h in hits]
        return "\n".join(lines)

    def timeline(self, since: float, until: float, *, session_id: str | None, turn_id: str | None) -> str:
        """Leafing through the diary: what happened in a period, with exact times."""
        now = self.clock.now()
        rows = self.db.query(
            "SELECT * FROM memories WHERE state <> 'superseded' AND occurred_start IS NOT NULL"
            " AND occurred_start < ? AND COALESCE(occurred_end, occurred_start) >= ? ORDER BY occurred_start, id LIMIT 60",
            (until, since),
        )
        memories = [_memory(r) for r in rows]
        if len(memories) > 30 and (until - since) > 2 * DAY:
            coarse = [m for m in memories if m.kind == "summary"] or [m for m in memories if m.importance >= 6]
            memories = coarse[:30] or memories[:30]
        self.log_retrievals(((m, self.r(m, now)) for m in memories), channel="timeline", session_id=session_id, turn_id=turn_id)
        days = self.db.query(
            "SELECT date(started_at, 'unixepoch', ?) AS day, COUNT(*) AS turns FROM turns WHERE started_at >= ? AND started_at < ? GROUP BY day ORDER BY day",
            (_utc_offset(self.tz, since), since, until),
        )
        lines = [f"{exact_label(since, None, self.tz)[:14]} 到 {exact_label(until - 1, None, self.tz)[:14]}："]
        lines += [self._exact_line(m, now) for m in memories] or ["（這段期間沒有整理過的記憶）"]
        if days:
            lines.append("那段時間的對話量：" + "、".join(f"{d['day'][5:].replace('-', '/')} {d['turns']} 回合" for d in days) + "（內容用 deep_recall 查）")
        return "\n".join(lines)

    def related(self, ref: str, *, session_id: str | None, turn_id: str | None, radius: int = 5) -> str:
        if m := re.fullmatch(r"m(\d+)", ref):
            return self._related_memory(int(m.group(1)), session_id=session_id, turn_id=turn_id)
        if m := re.fullmatch(r"msg#(\d+)", ref):
            return self._message_context(int(m.group(1)), radius)
        raise ValueError(f"看不懂的 ref：{ref}（例如 m12 或 msg#123）")

    def _related_memory(self, memory_id: int, *, session_id: str | None, turn_id: str | None) -> str:
        memory = self.get(memory_id)
        if memory is None:
            raise ValueError(f"找不到 m{memory_id}。")
        now = self.clock.now()
        lines = [self._exact_line(memory, now)]
        links = self.db.query(
            "SELECT l.relation, 'out' AS dir, l.dst_id AS other FROM memory_links l WHERE l.src_id = ?"
            " UNION ALL SELECT l.relation, 'in', l.src_id FROM memory_links l WHERE l.dst_id = ?",
            (memory_id, memory_id),
        )
        others = self.load(r["other"] for r in links)
        if links:
            lines.append("相關的記憶：")
            lines += [f"  {_RELATIONS.get((r['relation'], r['dir']), r['relation'])} {self._exact_line(others[r['other']], now)[2:]}" for r in links if r["other"] in others]
        sources = self.db.query(
            "SELECT msg.* FROM memory_sources s JOIN messages msg ON msg.id = s.message_id WHERE s.memory_id = ? ORDER BY msg.id LIMIT 40",
            (memory_id,),
        )
        if sources:
            lines.append("當時的對話：")
            lines += [_message_line(row, self.tz, mark="  ") for row in sources]
        self.log_retrievals([(memory, self.r(memory, now))] + [(m, self.r(m, now)) for m in others.values()], channel="related", session_id=session_id, turn_id=turn_id)
        return "\n".join(lines)

    def _message_context(self, message_id: int, radius: int) -> str:
        target = self.db.one("SELECT id, session_id FROM messages WHERE id = ?", (message_id,))
        if target is None:
            raise ValueError(f"找不到 msg#{message_id}。")
        before = self.db.query("SELECT * FROM messages WHERE session_id = ? AND id <= ? ORDER BY id DESC LIMIT ?", (target["session_id"], message_id, radius + 1))
        after = self.db.query("SELECT * FROM messages WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?", (target["session_id"], message_id, radius))
        lines = [f"msg#{message_id} 前後的對話："]
        lines += [_message_line(row, self.tz, mark="→ " if row["id"] == message_id else "  ") for row in [*reversed(before), *after]]
        return "\n".join(lines)

    # --- core block --------------------------------------------------------------------------------

    def core_block(self) -> str:
        """The always-present memory section of the system prompt, rebuilt whenever a session starts."""
        now = self.clock.now()
        rows = self.db.query("SELECT * FROM memories WHERE state IN ('labile', 'active') AND trust <> 'external'")
        live = [(m, self.r(m, now)) for m in map(_memory, rows)]

        def rank(items):
            return sorted(items, key=lambda item: -(item[1] * (0.5 + item[0].importance / 10)))

        about = rank([(m, r) for m, r in live if m.kind in ("preference", "fact") and m.trust == "owner"])[:10]
        known = rank([(m, r) for m, r in live if m.kind in ("fact", "insight") and m.trust == "self"])[:8]
        horizon = now + 7 * DAY
        plans = sorted(
            [(m, r) for m, r in live if m.kind == "intention" and (m.occurred_start is None or now - DAY <= m.occurred_start <= horizon)],
            key=lambda item: item[0].occurred_start or horizon,
        )[:8]
        alarms = self.db.query(
            "SELECT id, title, next_fire_at FROM alarms WHERE status = 'scheduled' AND next_fire_at <= ? ORDER BY next_fire_at LIMIT 10", (horizon,)
        )
        sections: list[tuple[str, list[str]]] = []

        def line(m: Memory, r: float) -> str:
            return f"- m{m.id} {m.content} [{time_label(m.occurred_start, m.time_precision, r, now, self.tz)}]"

        if about:
            sections.append(("## 關於主人", [line(m, r) for m, r in about]))
        if known:
            sections.append(("## 我知道的事", [line(m, r) for m, r in known]))
        if plans or alarms:
            plan_lines = [line(m, r) for m, r in plans]
            plan_lines += [f"- 鬧鐘 #{a['id']} {exact_label(a['next_fire_at'], None, self.tz)} {a['title']}" for a in alarms]
            sections.append(("## 近期計畫", plan_lines))
        if not sections:
            return ""
        out = [f"# 記憶（{datetime.fromtimestamp(now, self.tz):%m/%d %H:%M} 整理）"]
        size = len(out[0])
        shown: list[tuple[Memory, float]] = []
        by_ref = {m.id: (m, r) for m, r in live}
        for title, lines in sections:
            out.append(title)
            for text in lines:
                if size + len(text) > CORE_MAX_CHARS:
                    break
                out.append(text)
                size += len(text)
                if (ref := _CORE_REF.match(text)) and int(ref.group(1)) in by_ref:
                    shown.append(by_ref[int(ref.group(1))])
        self.log_retrievals(shown, channel="core", session_id=None, turn_id=None)
        self.db.execute(
            "INSERT INTO core_blocks(created_at, content, memory_ids) VALUES (?, ?, ?)",
            (now, "\n".join(out), json.dumps([m.id for m, _ in shown])),
        )
        return "\n".join(out)


_RELATIONS = {
    ("derived_from", "out"): "出自",
    ("derived_from", "in"): "衍生出",
    ("part_of", "out"): "屬於",
    ("part_of", "in"): "包含",
    ("continues", "out"): "接續",
    ("continues", "in"): "後續",
    ("similar", "out"): "類似",
    ("similar", "in"): "類似",
    ("abstracts", "out"): "歸納自",
    ("abstracts", "in"): "被歸納成",
    ("alarm", "out"): "鬧鐘",
    ("supersedes", "out"): "取代了",
    ("supersedes", "in"): "後來改成",
}
_LABELS = {"owner": "user", "alarm": "alarm", "system": "system", "process": "process"}


def _speaker(speaker: str | None) -> str:
    return _LABELS.get(speaker or "owner", "user")


def _clip(text: str, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _message_line(row, tz: ZoneInfo, *, mark: str) -> str:
    stamp = exact_label(row["ts"], None, tz)
    if row["role"] == "tool":
        return f"{mark}msg#{row['id']} [{stamp}]（{row['tool_name'] or '工具'} 的輸出，{len(row['content'] or '')} 字）"
    if not row["content"]:
        return f"{mark}msg#{row['id']} [{stamp}] niya 呼叫了工具"
    speaker = "niya" if row["role"] == "assistant" else _speaker(row["speaker"])
    return f"{mark}msg#{row['id']} [{stamp}] {speaker}: {_clip(row['content'])}"


def _utc_offset(tz: ZoneInfo, at: float) -> str:
    offset = datetime.fromtimestamp(at, tz).utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset else 0
    return f"{minutes:+d} minutes"
