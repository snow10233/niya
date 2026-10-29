"""The single consumer of the inbox: turns waiting items into agent turns, one at a time."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from niya.agent.budget import SOFT, over, percent
from niya.agent.loop import TurnResult
from niya.agent.render import format_ts
from niya.control import ModelControl
from niya.inbox import Inbox, InboundItem
from niya.llm import LLMError
from niya.tools.egress import EgressGate

log = logging.getLogger(__name__)

Say = Callable[[str], Awaitable[None]]
# Until sleep exists, a full context ends the session: (reason, percent used) -> start a new one.
ContextFull = Callable[[str, int], Awaitable[None]]
PROGRESS_CHARS = 800


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
    ) -> None:
        self.inbox = inbox
        self.control = control
        self.egress = egress
        self.reply = reply
        self.report = report
        self.on_context_full = on_context_full
        # System lines to put in front of the next turn (e.g. why the context was just cleared).
        self.carry: list[str] = []
        # Held for the whole turn; commands that swap the agent or the session take it too.
        self.lock = asyncio.Lock()

    async def run(self) -> None:
        while True:
            await self.inbox.wait()
            async with self.lock:
                items = self.inbox.take()
                if items:
                    await self.turn(items)

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
        used = percent(result.prompt_tokens, agent.context_limit)
        ended = format_ts(self.inbox.clock.now(), agent.conversation.tz)
        await self.on_context_full("context_full", used)
        note = f"上一段對話在 {ended} 因為 context 快滿了（{used}%）而結束，換成新的 session。之前的內容不在 context 裡，需要時用 deep_recall 找。"
        if result.context_full and result.text:
            note += f"\n結束前你交代的進度：{result.text[:PROGRESS_CHARS]}"
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
