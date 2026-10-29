"""Pure helpers for the Discord bot: who niya listens to, how often it says it is asleep, splitting long replies, tool progress."""

from collections.abc import Sequence

from niya.tools.preview import ToolView

DISCORD_LIMIT = 2000
PROGRESS_LIMIT = DISCORD_LIMIT - 64  # hermes leaves the same room for platform quirks
FILE_THRESHOLD = 6000  # longer replies go out as a .md attachment instead of a wall of messages
SLEEP_REPLY_INTERVAL_S = 30 * 60
_INDICATOR_RESERVE = 12
_FENCE_CLOSE = "\n```"


def should_handle(*, author_id: int, author_is_bot: bool, owner_id: int, is_dm: bool, channel_id: int, channel_ids: frozenset[int]) -> bool:
    """Only the owner, only in DMs or the channels set aside for niya; bots (niya included) never."""
    if author_is_bot or author_id != owner_id:
        return False
    return is_dm or channel_id in channel_ids


class SleepReplyLimiter:
    """Say "asleep" at most once per interval, so a burst of messages does not get a burst of replies."""

    def __init__(self, interval_s: float = SLEEP_REPLY_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self.last: float | None = None

    def allow(self, now: float) -> bool:
        if self.last is not None and now - self.last < self.interval_s:
            return False
        self.last = now
        return True


def _pieces(line: str, width: int) -> list[str]:
    return [line[i : i + width] for i in range(0, len(line), width)] or [""]


def chunk_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries; a code block cut in two is closed and reopened with its language."""
    if len(text) <= limit:
        return [text]
    budget = limit - _INDICATOR_RESERVE - len(_FENCE_CLOSE)
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    fence: str | None = None  # language of the code block we are inside, "" for a bare one

    def flush() -> None:
        body = "\n".join(current)
        chunks.append(body + (_FENCE_CLOSE if fence is not None else ""))

    for line in text.split("\n"):
        for piece in _pieces(line, budget - 20):
            if current and size + len(piece) + 1 > budget:
                flush()
                current = [f"```{fence}"] if fence is not None else []
                size = len(current[0]) + 1 if current else 0
            current.append(piece)
            size += len(piece) + 1
            stripped = piece.strip()
            if stripped.startswith("```"):
                fence = None if fence is not None else stripped[3:].strip()
    chunks.append("\n".join(current))
    total = len(chunks)
    return [f"{chunk}\n（{i}/{total}）" for i, chunk in enumerate(chunks, start=1)]



def render_progress(entries: Sequence[tuple[ToolView, int]]) -> str:
    """One progress message: a header per run of the same tool, each call's detail in its own code block."""
    parts: list[str] = []
    previous = None
    for view, count in entries:
        times = f" (×{count})" if count > 1 else ""
        if not view.detail:
            parts.append(view.line() + times)
            previous = None
            continue
        if view.header != previous:
            parts.append(view.header)
        parts.append(f"```\n{view.detail.replace('```', 'ˋˋˋ')}\n```{times}")
        previous = view.header
    return "\n".join(parts)
