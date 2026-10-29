"""What niya runs during a turn, as one Discord message that is edited as tool calls come in.

Follows the "accumulate" tool progress of NousResearch/hermes-agent (MIT) gateway@222465d84: a burst of calls
becomes a few edits of one message rather than a wall of messages. discord.py waits out rate limits itself.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from niya.discord_bot.text import render_progress

log = logging.getLogger(__name__)


class ProgressBubble:
    def __init__(self, channel: Callable[[], Awaitable[Any]]) -> None:
        self.channel = channel
        self.lines: list[str] = []
        self.message: Any = None
        self._lock = asyncio.Lock()
        self._queued = False
        self._writes: set[asyncio.Task] = set()

    def add(self, line: str) -> None:
        self.lines.append(line)
        if self._queued:  # a write already waiting for its turn picks this line up too
            return
        self._queued = True
        task = asyncio.create_task(self._write())
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    async def _write(self) -> None:
        async with self._lock:
            self._queued = False
            text = render_progress(self.lines)
            try:
                if self.message is None:
                    self.message = await (await self.channel()).send(text)
                else:
                    await self.message.edit(content=text)
            except Exception:
                log.warning("tool progress message failed", exc_info=True)

    async def flush(self) -> None:
        """Wait until every line added so far is on Discord, so a reply sent next shows up below it."""
        await asyncio.gather(*self._writes, return_exceptions=True)

    async def finish(self) -> None:
        """End of a turn: the next tool call starts a new message."""
        await self.flush()
        self.lines, self.message = [], None
