import json
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from niya.agent.render import render_history, strip_echoed_prefix
from niya.core.db import Database
from niya.core.inbox import InboundItem
from niya.llm.openrouter import ChatResult, ToolCall
from niya.memory.episodic import assign_segment, close_session_segments, finish_segment_turn, latest_compaction
from niya.tools.registry import ToolOutcome


@dataclass
class StoredMessage:
    id: int
    session_id: str
    turn_id: str | None
    role: str
    speaker: str | None
    content: str | None
    api_content: str | None
    tool_calls: list[dict[str, Any]] | None
    tool_call_id: str | None
    tool_name: str | None
    extra: dict[str, Any] | None
    ts: float


def new_id(prefix: str, ts: float, tz: ZoneInfo) -> str:
    return f"{prefix}-{datetime.fromtimestamp(ts, tz):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _loads(text: str | None) -> Any:
    return json.loads(text) if text else None


class Conversation:
    """Working memory: every message of the current session, persisted as it happens."""

    def __init__(self, db: Database, session_id: str, tz: ZoneInfo) -> None:
        self.db = db
        self.session_id = session_id
        self.tz = tz
        row = db.one("SELECT kind, system_prompt, engine, engine_session_id FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            raise ValueError(f"找不到 session：{session_id}")
        self.kind: str = row["kind"]
        self.system_prompt: str = row["system_prompt"]
        self.engine: str | None = row["engine"]
        self.engine_session_id: str | None = row["engine_session_id"]
        self.messages: list[StoredMessage] = [
            StoredMessage(
                id=r["id"],
                session_id=r["session_id"],
                turn_id=r["turn_id"],
                role=r["role"],
                speaker=r["speaker"],
                content=r["content"],
                api_content=r["api_content"],
                tool_calls=_loads(r["tool_calls"]),
                tool_call_id=r["tool_call_id"],
                tool_name=r["tool_name"],
                extra=_loads(r["extra"]),
                ts=r["ts"],
            )
            for r in db.query("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,))
        ]
        self.recall_blocks: dict[str, str] = {
            r["id"]: r["recall_block"]
            for r in db.query("SELECT id, recall_block FROM turns WHERE session_id = ? AND recall_block IS NOT NULL", (session_id,))
        }
        compaction = latest_compaction(db, session_id)
        # Messages up to context_start are out of the context; the handoff stands in for them.
        self.context_start: int = compaction["upto_message_id"] if compaction else 0
        self.handoff: str | None = compaction["handoff"] if compaction else None

    @classmethod
    def open_current(cls, db: Database, *, now: float, tz: ZoneInfo, system_prompt: Callable[[], str], kind: str = "main") -> "Conversation":
        row = db.one("SELECT id FROM sessions WHERE kind = ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1", (kind,))
        if row:
            return cls(db, row["id"], tz)
        return cls.start_new(db, now=now, tz=tz, system_prompt=system_prompt(), kind=kind)

    @classmethod
    def start_new(cls, db: Database, *, now: float, tz: ZoneInfo, system_prompt: str, kind: str = "main", parent_id: str | None = None) -> "Conversation":
        session_id = new_id(kind, now, tz)
        db.execute(
            "INSERT INTO sessions(id, kind, parent_id, started_at, system_prompt) VALUES (?, ?, ?, ?, ?)",
            (session_id, kind, parent_id, now, system_prompt),
        )
        return cls(db, session_id, tz)

    def end(self, *, now: float, reason: str) -> None:
        self.db.execute("UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?", (now, reason, self.session_id))
        close_session_segments(self.db, self.session_id)

    def bind_engine(self, engine: str, engine_session_id: str | None = None) -> None:
        """A session runs on one engine for its whole life; Claude Code also needs its own ID to resume."""
        self.engine = engine
        self.engine_session_id = engine_session_id or self.engine_session_id
        self.db.execute(
            "UPDATE sessions SET engine = ?, engine_session_id = ? WHERE id = ?",
            (self.engine, self.engine_session_id, self.session_id),
        )

    def compact(self, handoff: str, *, ts: float, prompt_tokens: int | None = None) -> None:
        """From now on the model sees the handoff instead of everything so far; a Claude Code session starts afresh."""
        upto = self.messages[-1].id if self.messages else 0
        self.db.execute(
            "INSERT INTO compactions(session_id, ts, upto_message_id, handoff, prompt_tokens) VALUES (?, ?, ?, ?, ?)",
            (self.session_id, ts, upto, handoff, prompt_tokens),
        )
        self.db.execute("UPDATE sessions SET engine_session_id = NULL WHERE id = ?", (self.session_id,))
        self.context_start, self.handoff, self.engine_session_id = upto, handoff, None

    @property
    def context_messages(self) -> list[StoredMessage]:
        return [m for m in self.messages if m.id > self.context_start]

    def with_handoff(self, content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        """The first user message after a compaction carries the handoff in front of it."""
        if not self.handoff:
            return content
        if isinstance(content, str):
            return f"{self.handoff}\n\n{content}"
        return [{"type": "text", "text": self.handoff}, *content]

    def start_turn(self, turn_id: str, *, source: str, ts: float, recall_block: str | None = None) -> None:
        last = self.db.one("SELECT MAX(ended_at) AS ended FROM turns WHERE session_id = ?", (self.session_id,))
        gap = ts - last["ended"] if last and last["ended"] is not None else None
        self.db.execute(
            "INSERT INTO turns(id, session_id, source, started_at, gap_s, recall_block) VALUES (?, ?, ?, ?, ?, ?)",
            (turn_id, self.session_id, source, ts, gap, recall_block),
        )
        if self.kind == "main":
            # Subagent work reaches memory through the parent's tool result, not as episodes of its own.
            assign_segment(self.db, session_id=self.session_id, turn_id=turn_id, source=source, ts=ts)
        if recall_block:
            self.recall_blocks[turn_id] = recall_block

    def set_recall_block(self, turn_id: str, block: str) -> None:
        """Fixed once written: the block becomes part of the turn's first user message for good."""
        self.db.execute("UPDATE turns SET recall_block = ? WHERE id = ?", (block, turn_id))
        self.recall_blocks[turn_id] = block

    def finish_turn(self, turn_id: str, *, ts: float) -> None:
        self.db.execute("UPDATE turns SET ended_at = ? WHERE id = ?", (ts, turn_id))
        finish_segment_turn(self.db, turn_id=turn_id, ts=ts)

    def add_user_items(self, turn_id: str, items: Sequence[InboundItem]) -> None:
        for item in sorted(items, key=lambda i: i.ts):
            images = item.meta.get("images")
            self._insert(turn_id=turn_id, role="user", speaker=item.speaker, content=item.content, extra={"images": list(images)} if images else None, ts=item.ts)

    def add_system_line(self, turn_id: str, text: str, *, ts: float) -> None:
        self._insert(turn_id=turn_id, role="user", speaker="system", content=text, ts=ts)

    def add_assistant(self, turn_id: str, result: ChatResult, *, ts: float) -> StoredMessage:
        content = strip_echoed_prefix(result.content).strip() if result.content else None
        extra = {"reasoning_details": result.reasoning_details} if result.reasoning_details else None
        return self._insert(
            turn_id=turn_id,
            role="assistant",
            speaker="niya",
            content=content or None,
            tool_calls=[call.to_api() for call in result.tool_calls] or None,
            extra=extra,
            ts=ts,
        )

    def add_tool_result(self, turn_id: str, outcome: ToolOutcome, *, ts: float) -> StoredMessage:
        return self._insert(
            turn_id=turn_id,
            role="tool",
            content=outcome.full_content,
            api_content=outcome.content,
            tool_call_id=outcome.call.id,
            tool_name=outcome.call.name,
            extra={"images": list(outcome.images)} if outcome.images else None,
            ts=ts,
        )

    def dangling_tool_calls(self) -> list[ToolCall]:
        answered = {m.tool_call_id for m in self.messages if m.role == "tool"}
        pending: list[ToolCall] = []
        for m in self.messages:
            for call in m.tool_calls or ():
                if call["id"] not in answered:
                    pending.append(ToolCall(id=call["id"], name=call["function"]["name"], arguments=call["function"]["arguments"]))
        return pending

    def close_dangling_tool_calls(self, turn_id: str, text: str, *, ts: float) -> None:
        for call in self.dangling_tool_calls():
            self.add_tool_result(turn_id, ToolOutcome(call=call, content=text, full_content=text, is_error=True), ts=ts)

    def api_messages(self) -> list[dict[str, Any]]:
        history = render_history(self.context_messages, self.tz, self.recall_blocks)
        if history and history[0]["role"] == "user":
            history[0] = {**history[0], "content": self.with_handoff(history[0]["content"])}
        return [{"role": "system", "content": self.system_prompt}, *history]

    def _insert(
        self,
        *,
        turn_id: str | None,
        role: str,
        ts: float,
        speaker: str | None = None,
        content: str | None = None,
        api_content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> StoredMessage:
        cursor = self.db.execute(
            "INSERT INTO messages(session_id, turn_id, role, speaker, content, api_content, tool_calls, tool_call_id, tool_name, extra, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.session_id,
                turn_id,
                role,
                speaker,
                content,
                api_content,
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                tool_call_id,
                tool_name,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                ts,
            ),
        )
        message = StoredMessage(
            id=cursor.lastrowid,
            session_id=self.session_id,
            turn_id=turn_id,
            role=role,
            speaker=speaker,
            content=content,
            api_content=api_content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            extra=extra,
            ts=ts,
        )
        self.messages.append(message)
        return message
