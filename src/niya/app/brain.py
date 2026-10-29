"""The single consumer of the inbox: turns waiting items into agent turns, one at a time."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from niya.agent.budget import SOFT, over, percent
from niya.agent.loop import TurnResult
from niya.app.control import ModelControl
from niya.core.inbox import InboundItem, Inbox
from niya.llm.openrouter import LLMError
from niya.tools.egress import EgressGate

log = logging.getLogger(__name__)

Say = Callable[[str], Awaitable[None]]
# (percent used, progress report if the turn was cut short) -> optionally a line to carry into the next turn.
ContextFull = Callable[[int, str], Awaitable[str | None]]


class Brain:
    def __init__(
        self,
        *,
        inbox: Inbox,
        control: ModelControl,
        egress: EgressGate,
        reply: Say,
        report: Say,
        on_context_full: ContextFull | None = None,
        on_busy: Callable[[bool], Awaitable[None]] | None = None,
    ) -> None:
        self.inbox = inbox
        self.control = control
        self.egress = egress
        self.reply = reply
        self.report = report
        self.on_context_full = on_context_full
        self.on_busy = on_busy
        self._current: asyncio.Task | None = None
        self._stopping = False
        # System lines to put in front of the next turn (e.g. why the context was just cleared).
        self.carry: list[str] = []
        # Held for the whole turn; commands that swap the agent or the session take it too.
        self.lock = asyncio.Lock()

    async def run(self) -> None:
        while True:
            await self.inbox.wait()
            async with self.lock:
                items = self.inbox.take()
                if not items:
                    continue
                if self.on_busy:
                    await self.on_busy(True)
                # Its own task, so /stop can cancel this turn without stopping the brain.
                self._current = asyncio.create_task(self.turn(items))
                try:
                    await self._current
                except asyncio.CancelledError:
                    if not self._stopping:
                        raise
                    self._stopping = False
                    await self.report("（已停下這一回合）")
                finally:
                    self._current = None
                    if self.on_busy:
                        await self.on_busy(False)

    def stop(self) -> bool:
        """Cancel the turn in progress (the inbox and everything else keep going)."""
        if self._current is None or self._current.done():
            return False
        self._stopping = True
        self._current.cancel()
        return True

    async def turn(self, items: list[InboundItem]) -> None:
        agent = self.control.agent
        try:
            result = await agent.run_turn(self.prepare(agent.conversation.session_id, items))
        except LLMError as exc:
            await self.report(f"[錯誤] {exc}")
            return
        except Exception:
            log.exception("turn failed")
            await self.report("[錯誤] 這一回合出了問題，細節在日誌裡。")
            return
        if result.text and not result.silent:
            await self.reply(result.text)
        await self.check_context(agent, result)

    async def check_context(self, agent, result: TurnResult) -> None:
        if self.on_context_full is None or not (result.context_full or over(result.prompt_tokens, agent.context_limit, SOFT)):
            return
        note = await self.on_context_full(percent(result.prompt_tokens, agent.context_limit), result.text if result.context_full else "")
        if note:
            self.carry.append(note)

    def prepare(self, session_id: str, items: list[InboundItem]) -> list[InboundItem]:
        """Owner messages may answer a paused upload and mention hosts; the gate sees them before the model does."""
        first = min(item.ts for item in items)
        out: list[InboundItem] = [InboundItem("system", note, first) for note in self.carry]
        self.carry.clear()
        owner_seen = False
        for item in items:
            if item.speaker == "owner":
                owner_seen = True
                self.egress.learn_owner_hosts(item.content)
                out += [InboundItem("system", note, item.ts) for note in self.egress.resolve_owner_reply(session_id, item.content)]
            out.append(item)
        if not owner_seen:
            out = [InboundItem("system", note, first) for note in self.egress.expired_notices(session_id)] + out
        return out

    async def idle(self) -> None:
        """Wait until nothing is queued or running."""
        while self.inbox.pending() or self.lock.locked():
            await asyncio.sleep(0.05)
