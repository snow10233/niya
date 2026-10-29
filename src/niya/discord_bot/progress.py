"""What niya runs during a turn, as a Discord message that is edited as tool calls come in.

Follows the "accumulate" tool progress of NousResearch/hermes-agent (MIT) gateway/run.py@222465d84: a burst of
calls becomes a few edits of one message; the same call again is counted (×N); once a message would pass the
length limit the next call starts another; and once niya says something, later calls go below it in a new one.
discord.py waits out rate limits itself.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from niya.discord_bot.text import PROGRESS_LIMIT, render_progress
from niya.tools.preview import ToolView

log = logging.getLogger(__name__)


@dataclass
class _Bubble:
    entries: list[tuple[ToolView, int]] = field(default_factory=list)
    message: Any = None
    dirty: bool = True


class ProgressBubble:
    def __init__(self, channel: Callable[[], Awaitable[Any]], limit: int = PROGRESS_LIMIT) -> None:
        self.channel = channel
        self.limit = limit
        self.bubbles: list[_Bubble] = []
        self._lock = asyncio.Lock()
        self._queued = False
        self._writes: set[asyncio.Task] = set()

    def add(self, view: ToolView) -> None:
        current = self.bubbles[-1] if self.bubbles else None
        if current and current.entries[-1][0] == view:
            current.entries[-1] = (view, current.entries[-1][1] + 1)
        elif current and len(render_progress([*current.entries, (view, 1)])) <= self.limit:
            current.entries.append((view, 1))
        else:
            current = _Bubble([(view, 1)])
            self.bubbles.append(current)
        current.dirty = True
        if self._queued:  # a write already waiting for its turn picks this call up too
            return
        self._queued = True
        task = asyncio.create_task(self._write())
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    async def _write(self) -> None:
        async with self._lock:
            self._queued = False
            for bubble in self.bubbles:
                if not bubble.dirty:
                    continue
                bubble.dirty = False
                text = render_progress(bubble.entries)
                try:
                    if bubble.message is None:
                        bubble.message = await (await self.channel()).send(text)
                    else:
                        await bubble.message.edit(content=text)
                except Exception:
                    log.warning("tool progress message failed", exc_info=True)

    async def flush(self) -> None:
        """Wait until every call added so far is on Discord."""
        await asyncio.gather(*self._writes, return_exceptions=True)

    async def finish(self) -> None:
        """niya is about to say something, or the turn ended: the next tool call starts a new message."""
        await self.flush()
        self.bubbles = []
