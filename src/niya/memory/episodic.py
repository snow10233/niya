"""Daytime segmentation of the raw record: a cheap, rule-based first cut that NREM later refines into events.

A chat segment runs until the conversation goes quiet for IDLE_GAP_S or the session ends. Turns started by
an alarm or a background process get a segment of their own, so they never get blended into a chat.
"""

from niya.core.db import Database

IDLE_GAP_S = 30 * 60
OWN_SEGMENT_SOURCES = frozenset({"alarm", "process"})


def _last_activity(db: Database, segment_id: int, started_at: float) -> float:
    row = db.one("SELECT MAX(COALESCE(ended_at, started_at)) AS last FROM turns WHERE segment_id = ?", (segment_id,))
    return row["last"] if row and row["last"] is not None else started_at


def close_segment(db: Database, segment_id: int, *, reason: str) -> None:
    db.execute("UPDATE segments SET status = 'closed', closed_reason = ? WHERE id = ? AND status = 'open'", (reason, segment_id))


def assign_segment(db: Database, *, session_id: str, turn_id: str, source: str, ts: float) -> int:
    """Put a new turn into a segment, opening one (and closing the stale one) when needed."""
    if source in OWN_SEGMENT_SOURCES:
        cursor = db.execute(
            "INSERT INTO segments(session_id, kind, started_at, ended_at, status, closed_reason) VALUES (?, ?, ?, ?, 'closed', ?)",
            (session_id, source, ts, ts, source),
        )
        segment_id = cursor.lastrowid
    else:
        current = db.one(
            "SELECT id, started_at FROM segments WHERE session_id = ? AND kind = 'chat' AND status = 'open' ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        segment_id = None
        if current is not None:
            if ts - _last_activity(db, current["id"], current["started_at"]) < IDLE_GAP_S:
                segment_id = current["id"]
            else:
                close_segment(db, current["id"], reason="idle")
        if segment_id is None:
            segment_id = db.execute("INSERT INTO segments(session_id, kind, started_at) VALUES (?, 'chat', ?)", (session_id, ts)).lastrowid
    db.execute("UPDATE turns SET segment_id = ? WHERE id = ?", (segment_id, turn_id))
    return segment_id


def finish_segment_turn(db: Database, *, turn_id: str, ts: float) -> None:
    db.execute("UPDATE segments SET ended_at = ? WHERE id = (SELECT segment_id FROM turns WHERE id = ?)", (ts, turn_id))


def close_session_segments(db: Database, session_id: str, *, reason: str = "session_end") -> None:
    db.execute("UPDATE segments SET status = 'closed', closed_reason = ? WHERE session_id = ? AND status = 'open'", (reason, session_id))


def close_idle_segments(db: Database, *, now: float) -> int:
    """Close chat segments that have been quiet for IDLE_GAP_S; returns how many."""
    closed = 0
    for row in db.query("SELECT id, started_at FROM segments WHERE status = 'open'"):
        if now - _last_activity(db, row["id"], row["started_at"]) >= IDLE_GAP_S:
            close_segment(db, row["id"], reason="idle")
            closed += 1
    return closed
