"""Everything that wants niya's attention goes through a persistent queue, consumed one turn at a time."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from niya.clock import Clock
from niya.db import Database

SPEAKERS = ("owner", "alarm", "system", "process")
PRIORITY = {"alarm": 30, "owner": 20, "system": 10, "process": 0}


@dataclass(frozen=True)
class InboundItem:
    speaker: str
    content: str
    ts: float
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.speaker not in SPEAKERS:
            raise ValueError(f"未知的說話者：{self.speaker}")


def turn_source(items: Sequence[InboundItem]) -> str:
    """A turn with anything the owner said is a chat turn; otherwise it belongs to whoever started it."""
    if any(item.speaker == "owner" for item in items):
        return "owner"
    return min(items, key=lambda item: item.ts).speaker


class Inbox:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock
        self._ready = asyncio.Event()
        if self.pending():
            self._ready.set()

    def put(self, speaker: str, content: str, *, ts: float | None = None, meta: dict[str, Any] | None = None) -> InboundItem:
        item = InboundItem(speaker, content, self.clock.now() if ts is None else ts, meta or {})
        self.db.execute(
            "INSERT INTO inbox(source, priority, content, meta, received_at) VALUES (?, ?, ?, ?, ?)",
            (item.speaker, PRIORITY[item.speaker], item.content, json.dumps(item.meta, ensure_ascii=False) if item.meta else None, item.ts),
        )
        self._ready.set()
        return item

    def pending(self) -> list[InboundItem]:
        rows = self.db.query("SELECT * FROM inbox WHERE taken_at IS NULL ORDER BY priority DESC, received_at, id")
        return [InboundItem(r["source"], r["content"], r["received_at"], json.loads(r["meta"]) if r["meta"] else {}) for r in rows]

    def take(self) -> list[InboundItem]:
        """The next batch: alarms first and on their own, otherwise everything that has been waiting."""
        rows = self.db.query("SELECT * FROM inbox WHERE taken_at IS NULL ORDER BY priority DESC, received_at, id")
        if rows and rows[0]["source"] == "alarm":
            rows = [r for r in rows if r["source"] == "alarm"]
        now = self.clock.now()
        with self.db.transaction():
            for r in rows:
                self.db.execute("UPDATE inbox SET taken_at = ? WHERE id = ?", (now, r["id"]))
        if not self.pending():
            self._ready.clear()
        return [InboundItem(r["source"], r["content"], r["received_at"], json.loads(r["meta"]) if r["meta"] else {}) for r in sorted(rows, key=lambda r: (r["received_at"], r["id"]))]

    async def wait(self) -> None:
        await self._ready.wait()
