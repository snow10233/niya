"""REM: connect new memories to what niya already knows, and draw insights from clusters of related ones.

Insights start weak (half the usual stability): they are niya's own guesses, and only hold up if they turn
out to be useful later.
"""

import logging
from dataclasses import dataclass, field

import numpy as np

from niya.memory.curve import initial_stability
from niya.memory.engine import Memory, MemoryEngine
from niya.memory.timefmt import exact_label
from niya.memory.vectors import from_blob
from niya.sleep.llm import Completer, parse_json
from niya.sleep.prompts import REM_SYSTEM
from niya.tools.threat_patterns import first_threat_message

log = logging.getLogger(__name__)

WATERMARK_KEY = "rem.last_memory_id"
SIMILAR_COS = 0.75
CLUSTER_COS = 0.6
MIN_CLUSTER = 3
MAX_INSIGHTS = 3
DUPLICATE_INSIGHT_COS = 0.9
KINDS = ("fact", "preference", "event", "intention")


@dataclass
class RemStats:
    linked: int = 0
    clusters: int = 0
    insights: list[str] = field(default_factory=list)
    reencountered: int = 0


def _vector(engine: MemoryEngine, memory_id: int) -> np.ndarray | None:
    row = engine.db.one("SELECT dim, vec FROM memory_vectors WHERE memory_id = ? AND model = ?", (memory_id, engine.embedder.model))
    return from_blob(row["vec"], row["dim"]) if row else None


async def rem(engine: MemoryEngine, completer: Completer, *, budget_left=lambda: True) -> RemStats:
    stats = RemStats()
    if engine.embedder is None:
        return stats
    # Memory ids only grow, so the last one REM looked at marks where to pick up next night.
    seen = int(engine.db.get_meta(WATERMARK_KEY) or 0)
    newest = engine.db.one("SELECT MAX(id) AS id FROM memories")["id"] or 0
    marks = ",".join("?" * len(KINDS))
    rows = engine.db.query(f"SELECT id FROM memories WHERE state = 'active' AND kind IN ({marks}) AND id > ? ORDER BY id", (*KINDS, seen))
    fresh = [m for m in engine.load(r["id"] for r in rows).values()]
    parent: dict[int, int] = {}

    def find(i: int) -> int:
        while parent.setdefault(i, i) != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    members: dict[int, Memory] = {}
    with engine.db.transaction():
        for memory in fresh:
            vec = _vector(engine, memory.id)
            if vec is None:
                continue
            members[memory.id] = memory
            for other, score in engine.nearest(vec, 8):
                if other.id == memory.id or other.kind == "summary":
                    continue
                if score >= SIMILAR_COS:
                    engine.add_link(memory.id, other.id, "similar", score)
                    stats.linked += 1
                if score >= CLUSTER_COS and other.kind in (*KINDS, "insight"):
                    members[other.id] = other
                    parent[find(memory.id)] = find(other.id)
    groups: dict[int, list[Memory]] = {}
    for memory_id, memory in members.items():
        groups.setdefault(find(memory_id), []).append(memory)
    clusters = sorted((g for g in groups.values() if len(g) >= MIN_CLUSTER and any(m.kind != "insight" for m in g)), key=len, reverse=True)
    stats.clusters = len(clusters)
    for cluster in clusters[:MAX_INSIGHTS]:
        if not budget_left():
            break
        await _insight(engine, completer, cluster, stats)
    engine.db.set_meta(WATERMARK_KEY, str(newest))
    return stats


async def _insight(engine: MemoryEngine, completer: Completer, cluster: list[Memory], stats: RemStats) -> None:
    listing = "\n".join(f"- [{exact_label(m.occurred_start, m.occurred_end, engine.tz)}] {m.content}" for m in cluster[:20])
    try:
        data = parse_json(await completer.complete(system=REM_SYSTEM, prompt=f"相關的記憶：\n{listing}", task="rem"))
    except ValueError as exc:
        log.warning("REM reply unusable: %s", exc)
        return
    text = str(data.get("insight") or "").strip() if isinstance(data, dict) else ""
    if not text or text.lower() == "null" or first_threat_message(text, "strict"):
        return
    vectors = await engine.embed([text])
    with engine.db.transaction():
        if vectors is not None:
            near = [(m, s) for m, s in engine.nearest(vectors[0], 3) if m.kind == "insight" and s >= DUPLICATE_INSIGHT_COS]
            if near:
                engine.review(near[0][0], kind="reencounter", ref_id=f"rem-{int(engine.clock.now())}", at=engine.clock.now())
                stats.reencountered += 1
                return
        importance = min(max(int(data.get("importance") or 4), 1), 10)
        insight = engine.create(
            kind="insight",
            content=text[:600],
            importance=importance,
            occurred_start=min(m.occurred_start or engine.clock.now() for m in cluster),
            occurred_end=max(m.occurred_end or m.occurred_start or engine.clock.now() for m in cluster),
            precision="week",
            origin="rem",
            stability=initial_stability(importance, engine.params) * engine.params.insight_factor,
            vector=vectors[0] if vectors else None,
        )
        for member in cluster:
            engine.add_link(insight.id, member.id, "abstracts")
    stats.insights.append(text)
