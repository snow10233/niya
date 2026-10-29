"""One running niya, whatever the owner talks to it through (the terminal REPL or Discord).

The interface only moves text: owner messages go into the inbox, and niya's replies and system notices
come back through the callbacks. Every owner command is a method here that returns the text to show.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from niya.agent.budget import percent
from niya.agent.render import format_ts
from niya.app.brain import Brain
from niya.app.control import ModelControl
from niya.app.runtime import (
    build_agent,
    build_core_tools,
    connect_mcp,
    needs_new_session,
    open_conversation,
    process_notice,
    restart_conversation,
    sleep_completer,
    system_prompt_factory,
)
from niya.core.clock import Clock, FakeClock, RealClock
from niya.core.config import Settings
from niya.core.db import Database
from niya.core.inbox import Inbox
from niya.llm.choice import current_choice
from niya.sleep.orchestrator import KIND_LABELS, SleepOrchestrator

Say = Callable[[str], Awaitable[None]]


@dataclass
class Interface:
    channel: str  # where replies end up, as told to niya in its system prompt
    say: Say  # niya's own replies
    notice: Say  # system notices: paused uploads, waking up, errors
    confirm: Callable[[str], Awaitable[bool]]  # yes/no questions (e.g. switching provider)
    on_busy: Callable[[bool], Awaitable[None]] | None = None


class Niya:
    def __init__(self, settings: Settings, interface: Interface, *, clock: Clock | None = None) -> None:
        self.settings = settings
        self.ui = interface
        self.clock = clock or RealClock()
        self.tasks: list[asyncio.Task] = []
        self.started = False

    async def start(self) -> list[str]:
        """Assemble and start everything; returns notes worth telling the owner about."""
        s, ui = self.settings, self.ui
        notes: list[str] = []
        self.db = Database(s.db_path)
        self.inbox = Inbox(self.db, self.clock)
        self.tools = build_core_tools(s, self.db, self.clock, notify_owner=ui.notice, on_process_exit=lambda bg: self.inbox.put("process", process_notice(bg)))
        mcp = await connect_mcp(s, self.tools)
        notes += [f"MCP server {name} 連不上，先略過：{error}" for name, error in mcp.errors.items()]
        self.prompt = system_prompt_factory(s, channel=ui.channel, core_block=self.tools.memory.core_block, skills_index=self.tools.skills.index)
        choice = current_choice(self.db, s)[0]
        conv = open_conversation(self.db, s, self.clock, self.prompt)
        agent = build_agent(s, self.db, self.clock, self.tools, conv, choice)
        if needs_new_session(conv, choice):
            agent.conversation = restart_conversation(conv, s, self.clock, self.prompt, reason="engine_switch")
            notes.append(f"模型換成 {choice.provider}，已開新 session。")
        self.control = ModelControl(settings=s, db=self.db, clock=self.clock, tools=self.tools, prompt=self.prompt, agent=agent, confirm=ui.confirm)
        self.brain = Brain(
            inbox=self.inbox, control=self.control, egress=self.tools.egress, reply=ui.say, report=ui.notice, on_context_full=self._context_full, on_busy=ui.on_busy
        )
        self.sleep = SleepOrchestrator(
            db=self.db,
            clock=self.clock,
            tz=s.tz,
            engine=self.tools.memory,
            completer=sleep_completer(s, self.db, self.clock, self.tools),
            brain=self.brain,
            current_session=lambda: self.control.agent.conversation.session_id,
            wake_up=self._wake_up,
            notify=ui.notice,
            backups_dir=s.home / "backups",
            budget_usd=s.sleep_budget_usd,
            skills=self.tools.skills,
        )
        self.tasks = [
            asyncio.create_task(self.brain.run()),
            asyncio.create_task(self.tools.alarms.run(self.inbox, on_fire=self._alarm_rang)),
            asyncio.create_task(self.sleep.run()),
        ]
        self.started = True
        if pending := len(self.inbox.pending()):
            notes.append(f"還有 {pending} 則沒處理完的訊息，先交給 niya。")
        return notes

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.started:
            await self.control.agent.aclose()
            await self.tools.close()
            self.db.close()

    # --- callbacks ---------------------------------------------------------------------------------

    async def _context_full(self, used: int, progress: str) -> None:
        await self.ui.say(f"有點累了，先睡一下，整理完就醒。（context 用了 {used}%）")
        await self.sleep.request_sleep("overflow", progress=progress)

    async def _wake_up(self, kind: str, reason: str, line: str) -> None:
        agent = self.control.agent
        agent.conversation = restart_conversation(agent.conversation, self.settings, self.clock, self.prompt, reason="sleep")
        self.brain.carry.append(line)
        await self.ui.notice(f"（niya {line}）")

    async def _alarm_rang(self, fired: list[Any]) -> None:
        if self.sleep.asleep and any(alarm.wake_from_sleep for alarm in fired):
            await self.ui.notice("（鬧鐘響了，叫醒 niya）")
            await self.sleep.wake("alarm")

    # --- owner input -------------------------------------------------------------------------------

    @property
    def asleep(self) -> bool:
        return self.sleep.asleep

    def owner_says(self, text: str, meta: dict[str, Any] | None = None) -> str | None:
        """Queue an owner message; returns a note to show when niya cannot answer right away."""
        self.inbox.put("owner", text, meta=meta)
        return "（niya 睡覺中，整理完就醒，訊息先排隊；急事用 /wake）" if self.asleep else None

    async def idle(self) -> None:
        await self.brain.idle()

    async def settle(self) -> None:
        """Wait until any sleep and every queued turn are done."""
        await asyncio.sleep(0)
        while self.asleep:
            await asyncio.sleep(0.05)
        await self.brain.idle()

    # --- commands ----------------------------------------------------------------------------------

    def status(self) -> str:
        agent, db = self.control.agent, self.db
        now = self.clock.now()
        lines = [
            f"現在 {format_ts(now, self.settings.tz)}，session {agent.conversation.session_id}",
            self.control.status(),
            f"context：{agent.last_prompt_tokens:,} / {agent.context_limit:,} tokens（{percent(agent.last_prompt_tokens, agent.context_limit)}%）",
        ]
        if self.asleep:
            stage = db.one("SELECT kind, stage FROM sleep_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1")
            lines.append(f"睡覺中（{KIND_LABELS.get(stage['kind'], stage['kind'])}，{stage['stage']}）" if stage else "睡覺中")
        else:
            lines.append(f"醒著；下一次睡覺：{self.sleep.next_slot(now)}")
            if db.get_meta("sleep.resume"):
                lines.append("上次的睡眠還沒整理完，閒下來會續睡。")
        memories = db.one("SELECT COUNT(*) AS n FROM memories WHERE state IN ('labile', 'active')")["n"]
        lines.append(f"鬧鐘 {len(self.tools.alarms.active())} 個；長期記憶 {memories} 條；技能 {len(self.tools.skills.all(('active',)))} 個")
        return "\n".join(lines)

    def dream(self) -> str:
        row = self.db.one("SELECT created_at, content FROM dream_journal ORDER BY id DESC LIMIT 1")
        return f"{format_ts(row['created_at'], self.settings.tz)} 的夢：\n{row['content']}" if row else "還沒有夢境日誌（夜間睡眠後才會有）。"

    def alarms(self, arg: str = "") -> str:
        alarms = self.tools.alarms
        parts = arg.split()
        if not parts:
            active = alarms.active()
            return "\n".join(alarms.describe(a) for a in active) if active else "目前沒有鬧鐘。"
        if len(parts) == 2 and parts[0] == "cancel" and parts[1].lstrip("#").isdigit():
            try:
                cancelled = alarms.cancel(int(parts[1].lstrip("#")))
            except ValueError as exc:
                return str(exc)
            return f"已取消 #{cancelled.id}「{cancelled.title}」"
        return "用法：/alarms 或 /alarms cancel <id>"

    def egress(self, arg: str = "") -> str:
        gate = self.tools.egress
        parts = arg.split()
        if parts == ["list"] or not parts:
            return "\n".join(f"{host}（{source}）" for host, source in gate.known_hosts()) or "目前沒有已知主機。"
        if len(parts) == 2 and parts[0] == "allow":
            gate.allow(parts[1])
            return f"已加入已知主機：{parts[1]}"
        if len(parts) == 2 and parts[0] == "remove":
            return f"已移除：{parts[1]}" if gate.remove(parts[1]) else f"清單裡沒有 {parts[1]}"
        return "用法：/egress list、/egress allow <host>、/egress remove <host>"

    async def _locked(self, action: Callable[[], Awaitable[str]]) -> str:
        if self.asleep:
            return "niya 睡覺中，醒來再改（或先 /wake）。"
        async with self.brain.lock:
            return await action()

    async def model(self, arg: str = "") -> str:
        return await self._locked(lambda: self.control.model(arg))

    async def effort(self, arg: str = "") -> str:
        return await self._locked(lambda: self.control.effort(arg))

    async def new_session(self) -> str:
        async def restart() -> str:
            agent = self.control.agent
            agent.conversation = restart_conversation(agent.conversation, self.settings, self.clock, self.prompt, reason="manual")
            return f"已開新 session：{agent.conversation.session_id}"

        return await self._locked(restart)

    async def go_to_sleep(self, kind: str = "") -> str:
        if kind and kind not in KIND_LABELS:
            return "用法：/sleep、/sleep nap、/sleep night"
        return await self.sleep.request_sleep("manual", kind=kind or None)

    async def wake(self) -> str:
        return "叫醒了，還沒整理完的部分之後會找空檔續睡。" if await self.sleep.wake("owner") else "niya 醒著。"

    def stop(self) -> str:
        return "好，停下來了。" if self.brain.stop() else "niya 現在沒有在做事。"

    async def advance(self, seconds: float) -> str:
        """Fast-forward a fake clock and let alarms and sleep catch up (testing only)."""
        if not isinstance(self.clock, FakeClock):
            return "只有快轉時鐘模式能用。"
        self.clock.advance(seconds)
        fired = self.tools.alarms.fire_due(self.inbox, self.clock.now())
        if fired:
            await self._alarm_rang([self.tools.alarms.get(i) for i in fired])
        await self.sleep.tick()
        return f"現在是 {format_ts(self.clock.now(), self.settings.tz)}"
