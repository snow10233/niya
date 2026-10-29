"""Hermes' conversation history → niya's raw record, cut the way niya would have lived it.

Each Hermes session is split at every scheduled sleep (03:00, 13:00), because that is where niya clears its
context. What Hermes injected for itself is stripped: gateway headers, compaction summaries, heartbeat
prompts (a heartbeat that ended in silence is dropped altogether). Rewound turns, which the owner took back,
never happened. Cron and subagent sessions stay out: their results reached the owner through the main chat.

Imported segments wait in status 'replay' until the replay's clock reaches them (see replay.py).
"""

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from niya.agent.context import Conversation
from niya.core.db import Database
from niya.core.inbox import InboundItem
from niya.llm.openrouter import ChatResult, ToolCall, Usage
from niya.tools.registry import ToolOutcome, truncate

SKIPPED_SOURCES = frozenset({"cron", "subagent"})
SLEEP_HOURS = (3, 13)  # niya's scheduled sleeps; a session never runs across one
NOTICE_CHARS = 300
TOOL_API_CHARS = 4000  # what the model would have been shown; the full output stays in content
SILENT = "[SILENT]"

_ORIGIN = re.compile(r"\AGateway message origin \(JSON data[^\n]*\n\{[^\n]*\}\n(?:Do not guess a reply destination[^\n]*\n)?")
# Lines Hermes put in front of what the owner said; the owner's own display tag (e.g. "[snow]") is found from the data.
_HEADER = re.compile(
    r"\A(?:\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d\d-\d\d [\d:]+ [A-Z]+\]"
    r"|\[Triggering message id:[^\]\n]*\]"
    r"|\[OUT-OF-BAND USER MESSAGE[^\]\n]*\]"
    r"|\[System note:[^\]\n]*\]"
    r"|\[Note: model was just switched[^\]\n]*\])\s*"
)
_NEW_MESSAGE = "\n[New message]\n"
_TAG = re.compile(r"\A\[([^\[\]\n]{1,32})\] ")
# (marker, speaker): Hermes' own notices, kept short so the conversation around them stays readable.
_NOTICES = (
    ("[IMPORTANT: Background process", "process"),
    ("[IMPORTANT:", "system"),
    ("[ASYNC DELEGATION", "system"),
    ("[Cron delivery:", "system"),
)
HEARTBEAT_LINE = "（心跳：主動找主人說話）"


@dataclass
class Row:
    role: str
    ts: float
    speaker: str | None = None
    content: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass
class Turn:
    hermes_session: str
    hermes_id: int  # the first Hermes message of the turn; stable across re-imports
    source: str  # owner / heartbeat / notice
    rows: list[Row] = field(default_factory=list)

    @property
    def started_at(self) -> float:
        return self.rows[0].ts

    @property
    def ended_at(self) -> float:
        return self.rows[-1].ts

    def answered(self) -> bool:
        return any(r.role == "assistant" or r.role == "tool" for r in self.rows)

    def says_something(self) -> bool:
        return any(r.role == "assistant" and r.content for r in self.rows)


@dataclass
class ImportStats:
    sessions: int = 0
    turns: int = 0
    messages: int = 0
    segments: int = 0
    dropped_heartbeats: int = 0
    first: float | None = None
    last: float | None = None


def _connect(state_db: Path) -> sqlite3.Connection:
    # immutable: never touch the copy (no -wal/-shm files, no locks)
    conn = sqlite3.connect(f"file:{state_db}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _strip(text: str, tag: str | None) -> str:
    if _NEW_MESSAGE in text:
        text = text.rsplit(_NEW_MESSAGE, 1)[1]  # "[Recent channel messages] … [New message] <what was said>"
    text = _ORIGIN.sub("", text.lstrip()).lstrip()
    while True:
        stripped = _HEADER.sub("", text)
        if tag and stripped.startswith(f"[{tag}] "):
            stripped = stripped[len(tag) + 3 :]
        if stripped == text:
            return text.strip()
        text = stripped


def owner_tag(conn: sqlite3.Connection) -> str | None:
    """The display tag Hermes put before the owner's words on group platforms (most common leading [x])."""
    counts: Counter[str] = Counter()
    total = 0
    for (content,) in conn.execute("SELECT content FROM messages WHERE role = 'user' AND content IS NOT NULL LIMIT 5000"):
        total += 1
        body = _HEADER.sub("", _ORIGIN.sub("", content.lstrip()))
        if match := _TAG.match(body):
            counts[match.group(1)] += 1
    if not counts:
        return None
    tag, n = counts.most_common(1)[0]
    return tag if n >= total * 0.1 else None


def _classify(content: str, display_kind: str | None, tag: str | None) -> tuple[str, str, str] | None:
    """(turn source, speaker, text) for one Hermes user message, or None when it was Hermes talking to itself."""
    text = _strip(content, tag)
    if not text or text.startswith("[CONTEXT COMPACTION"):
        return None
    if text.startswith("[Heartbeat"):
        return "heartbeat", "alarm", HEARTBEAT_LINE
    for marker, speaker in _NOTICES:
        if text.startswith(marker):
            return "notice", speaker, text if len(text) <= NOTICE_CHARS else text[:NOTICE_CHARS] + "…"
    if display_kind == "steer":
        return "steer", "owner", text
    return "owner", "owner", text


def _tool_calls(raw: str | None) -> list[dict] | None:
    if not raw:
        return None
    calls = [
        {"id": c.get("id") or c.get("call_id") or "", "type": "function", "function": {"name": c["function"].get("name", ""), "arguments": c["function"].get("arguments") or ""}}
        for c in json.loads(raw)
        if isinstance(c, dict) and isinstance(c.get("function"), dict)
    ]
    return calls or None


def read_turns(state_db: Path) -> tuple[list[Turn], int]:
    """Every turn worth keeping, in time order; also how many silent heartbeats were dropped."""
    conn = _connect(state_db)
    try:
        tag = owner_tag(conn)
        marks = ",".join("?" * len(SKIPPED_SOURCES))
        rows = conn.execute(
            "SELECT m.* FROM messages m JOIN sessions s ON s.id = m.session_id"
            f" WHERE s.source NOT IN ({marks}) AND NOT (m.active = 0 AND m.compacted = 0) AND m.role IN ('user', 'assistant', 'tool')"
            " ORDER BY s.started_at, m.session_id, m.id",
            tuple(SKIPPED_SOURCES),
        ).fetchall()
    finally:
        conn.close()
    turns: list[Turn] = []
    current: Turn | None = None
    for m in rows:
        if current is not None and current.hermes_session != m["session_id"]:
            current = None
        if m["role"] == "user":
            kind = _classify(m["content"] or "", m["display_kind"], tag)
            if kind is None:
                continue
            source, speaker, text = kind
            row = Row("user", m["timestamp"], speaker=speaker, content=text)
            if source == "steer" and current is not None:
                current.rows.append(row)  # said while niya was still working: part of the same turn
                continue
            source = "owner" if source == "steer" else source
            if current is not None and not current.answered() and current.source == source:
                current.rows.append(row)
                continue
            current = Turn(m["session_id"], m["id"], source, [row])
            turns.append(current)
        elif current is not None:
            content = (m["content"] or "").strip() or None
            if m["role"] == "assistant":
                current.rows.append(Row("assistant", m["timestamp"], speaker="niya", content=None if content == SILENT else content, tool_calls=_tool_calls(m["tool_calls"])))
            else:
                current.rows.append(Row("tool", m["timestamp"], content=content or "", tool_call_id=m["tool_call_id"], tool_name=m["tool_name"]))
    kept = [t for t in turns if t.source != "heartbeat" or t.says_something()]
    return sorted(kept, key=lambda t: (t.started_at, t.hermes_id)), len(turns) - len(kept)


def waking_period(ts: float, tz: ZoneInfo) -> datetime:
    """The scheduled sleep this moment comes after; everything until the next one is one waking session."""
    moment = datetime.fromtimestamp(ts, tz)
    for days in range(2):
        day = moment - timedelta(days=days)
        for hour in sorted(SLEEP_HOURS, reverse=True):
            start = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if start <= moment:
                return start
    raise AssertionError("unreachable")


def import_turns(db: Database, turns: Iterable[Turn], *, tz: ZoneInfo, redact: Callable[[str], str]) -> ImportStats:
    """Turns already imported are skipped, so a fresher copy of state.db can be imported on top of an older one."""
    groups: dict[tuple[str, datetime], list[Turn]] = {}
    for turn in turns:
        if db.one("SELECT 1 FROM turns WHERE id = ?", (f"hermes-{turn.hermes_id}",)) is None:
            groups.setdefault((turn.hermes_session, waking_period(turn.started_at, tz)), []).append(turn)
    stats = ImportStats()
    with db.transaction():
        for (hermes_session, period), group in sorted(groups.items(), key=lambda item: item[1][0].started_at):
            session_id = f"hermes-{period:%Y%m%d-%H%M}-{hermes_session}"
            db.execute(
                "INSERT INTO sessions(id, kind, started_at, ended_at, end_reason, system_prompt, imported_from) VALUES (?, 'main', ?, ?, 'import', '', ?)"
                " ON CONFLICT(id) DO UPDATE SET ended_at = MAX(ended_at, excluded.ended_at)",
                (session_id, group[0].started_at, group[-1].ended_at, f"hermes:{hermes_session}"),
            )
            conv = Conversation(db, session_id, tz)
            for turn in group:
                turn_id = f"hermes-{turn.hermes_id}"
                conv.start_turn(turn_id, source=turn.source, ts=turn.started_at)
                for row in turn.rows:
                    _store(conv, turn_id, row, redact)
                conv.finish_turn(turn_id, ts=turn.ended_at)
                stats.messages += len(turn.rows)
            # Waiting for the replay; until then sleep leaves them alone.
            stats.segments += db.execute(
                "UPDATE segments SET status = 'replay' WHERE session_id = ? AND (status = 'open' OR (status = 'closed' AND closed_reason = 'idle'))", (session_id,)
            ).rowcount
            stats.sessions += 1
            stats.turns += len(group)
            stats.first = min(stats.first or group[0].started_at, group[0].started_at)
            stats.last = max(stats.last or 0.0, group[-1].ended_at)
    return stats


def _store(conv: Conversation, turn_id: str, row: Row, redact: Callable[[str], str]) -> None:
    content = redact(row.content) if row.content else row.content
    if row.role == "user":
        conv.add_user_items(turn_id, [InboundItem(row.speaker or "owner", content or "", row.ts)])
    elif row.role == "assistant":
        calls = tuple(ToolCall(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in row.tool_calls or ())
        conv.add_assistant(turn_id, ChatResult(content=content, tool_calls=calls, usage=Usage(), model="hermes"), ts=row.ts)
    else:
        call = ToolCall(row.tool_call_id or "", row.tool_name or "", "")
        conv.add_tool_result(turn_id, ToolOutcome(call=call, content=truncate(content or "", TOOL_API_CHARS), full_content=content or ""), ts=row.ts)
