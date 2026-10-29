import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import TZ, FakeLLM, HashEmbedder, reply, ts

from niya.agent.context import Conversation
from niya.agent.loop import Agent
from niya.app.brain import Brain
from niya.core.clock import FakeClock
from niya.core.db import Database
from niya.core.inbox import Inbox
from niya.memory.engine import MemoryEngine
from niya.sleep.orchestrator import SleepOrchestrator
from niya.tools.egress import EgressDetector, EgressGate
from niya.tools.registry import ToolRegistry


class AutoCompleter:
    """Cuts every segment into one event, summarises the day, dreams a line; can be made to hang."""

    def __init__(self):
        self.calls = []
        self.hold: asyncio.Event | None = None

    async def complete(self, *, system, prompt, task):
        self.calls.append(task)
        if self.hold is not None:
            await self.hold.wait()
        if task == "nrem":
            labels = re.findall(r"^\[(t\d+)\]", prompt, re.M)
            first = re.search(r"user: (.+)", prompt).group(1)
            return json.dumps({"events": [{"turns": labels, "title": first[:20], "summary": first, "outcome": "聊天", "importance": 4, "memories": []}], "used_memories": []})
        if task == "day_summary":
            return json.dumps({"summary": "主人和 niya 聊了一些事。", "importance": 4})
        return "我夢見和主人聊天。"


class SleepTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = Database(":memory:")
        self.clock = FakeClock(ts("2026-10-29 12:00"))
        self.engine = MemoryEngine(self.db, self.clock, TZ, embedder=HashEmbedder())
        self.inbox = Inbox(self.db, self.clock)
        self.llm = FakeLLM()
        conv = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S1")
        self.agent = Agent(llm=self.llm, model="m", tools=ToolRegistry(), conversation=conv, clock=self.clock)
        self.said, self.woke = [], []

        async def say(text):
            self.said.append(text)

        self.brain = Brain(inbox=self.inbox, control=SimpleNamespace(agent=self.agent), egress=EgressGate(self.db, now=self.clock.now, detector=EgressDetector()), reply=say, report=say)
        self.completer = AutoCompleter()

        async def wake_up(kind, reason, line):
            self.woke.append((kind, reason, line))
            self.agent.conversation.end(now=self.clock.now(), reason="sleep")
            self.agent.conversation = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S2")
            self.brain.carry.append(line)

        self.sleep = SleepOrchestrator(
            db=self.db, clock=self.clock, tz=TZ, engine=self.engine, completer=lambda: self.completer, brain=self.brain,
            current_session=lambda: self.agent.conversation.session_id, wake_up=wake_up, notify=say, backups_dir=self.dir / "backups",
        )
        self.brain_task = asyncio.create_task(self.brain.run())

        async def stop():
            self.brain_task.cancel()
            if self.sleep.task:
                self.sleep.task.cancel()
            await asyncio.gather(self.brain_task, self.sleep.task or asyncio.sleep(0), return_exceptions=True)

        self.addAsyncCleanup(stop)

    async def chat(self, text, answer="嗯"):
        self.llm.results.append(reply(answer))
        self.inbox.put("owner", text)
        await asyncio.sleep(0)
        await asyncio.wait_for(self.brain.idle(), 2)
        self.clock.advance(60)

    async def until_awake(self):
        if self.sleep.task:
            await asyncio.wait_for(asyncio.gather(self.sleep.task, return_exceptions=True), 5)

    def runs(self):
        return [(r["kind"], r["reason"], r["slot"], r["replaced_slot"], r["status"]) for r in self.db.query("SELECT * FROM sleep_runs ORDER BY id")]

    async def test_scheduled_nap_consolidates_and_clears_the_context(self):
        await self.chat("幫我查一下天氣")
        old_session = self.agent.conversation.session_id
        self.clock.set(ts("2026-10-29 13:00"))
        await self.sleep.tick()
        self.assertTrue(self.sleep.asleep)
        await self.until_awake()
        self.assertEqual(self.runs(), [("nap", "scheduled", "nap@2026-10-29T13:00", None, "done")])
        self.assertNotEqual(self.agent.conversation.session_id, old_session)
        self.assertEqual(self.woke[0][2], "午睡醒來（整理了 1 個事件、0 條新記憶，封存 0 條）。之前的對話已經整理成記憶，context 清空了。")
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM memories WHERE kind = 'event'")["n"], 1)
        await self.chat("醒了嗎", answer="醒了")
        first = self.llm.requests[-1]["messages"]
        self.assertEqual(len(first), 2)
        self.assertIn("system: 午睡醒來", first[1]["content"].splitlines()[0])
        self.clock.set(ts("2026-10-30 13:00"))
        await self.sleep.tick()
        self.assertFalse(self.sleep.asleep)  # nothing happened since waking: the nap is skipped
        self.assertEqual(self.runs()[-1][-1], "skipped")

    async def test_night_writes_a_day_summary_a_dream_and_a_backup(self):
        await self.chat("今天把 nginx 弄好了")
        self.clock.set(ts("2026-10-30 03:00"))
        await self.sleep.tick()
        await self.until_awake()
        summary = self.db.one("SELECT * FROM memories WHERE kind = 'summary'")
        self.assertEqual((summary["level"], summary["content"]), ("day", "10/29：主人和 niya 聊了一些事。"))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM memory_links WHERE relation = 'part_of'")["n"], 1)
        self.assertEqual(self.db.one("SELECT content FROM dream_journal")["content"], "我夢見和主人聊天。")
        self.assertEqual(len(list((self.dir / "backups").glob("niya-*.db"))), 1)

    async def test_budget_stops_summaries_and_rem_but_never_nrem(self):
        await self.chat("今天把 nginx 弄好了")
        self.sleep.budget_usd = 0
        self.clock.set(ts("2026-10-30 03:00"))
        await self.sleep.tick()
        await self.until_awake()
        stats = json.loads(self.db.one("SELECT stats FROM sleep_runs")["stats"])
        self.assertEqual((stats["events"], stats["summary"], stats["over_budget"], stats["finished"]), (1, False, True, True))
        self.assertNotIn("day_summary", self.completer.calls)
        self.assertEqual(len(self.woke), 1)

    async def test_missed_slots_are_not_made_up_later(self):
        self.clock.set(ts("2026-10-29 10:00"))  # 03:00 was 7 hours ago
        self.assertIsNone(self.sleep.due_slot(self.clock.now()))

    async def test_early_sleep_replaces_the_next_slot(self):
        await self.chat("很長的工作")
        self.clock.set(ts("2026-10-29 10:00"))
        await self.sleep.request_sleep("overflow", progress="正在改 nginx 設定，還沒重啟")
        await self.until_awake()
        self.assertEqual(self.runs()[0][:4], ("nap", "overflow", None, "nap@2026-10-29T13:00"))
        self.assertIn("提早睡醒來（context 滿了", self.woke[0][2])
        self.assertIn("睡前正在做：正在改 nginx 設定，還沒重啟", self.woke[0][2])
        self.clock.set(ts("2026-10-29 13:00"))
        await self.sleep.tick()
        self.assertFalse(self.sleep.asleep)
        await self.chat("又一個長工作")
        self.clock.set(ts("2026-10-29 18:00"))
        await self.sleep.request_sleep("overflow")
        await self.until_awake()
        self.assertEqual(self.runs()[-1][:4], ("night", "overflow", None, "night@2026-10-30T03:00"))
        self.clock.set(ts("2026-10-30 03:00"))
        await self.sleep.tick()
        self.assertFalse(self.sleep.asleep)

    async def test_woken_up_keeps_the_context_and_resumes_when_quiet(self):
        await self.chat("我們聊了點東西")
        session = self.agent.conversation.session_id
        self.completer.hold = asyncio.Event()
        await self.sleep.request_sleep("manual", kind="nap")
        await asyncio.sleep(0.05)
        self.llm.results.append(reply("我在"))
        self.inbox.put("owner", "在嗎")
        self.assertTrue(await self.sleep.wake("owner"))
        self.assertEqual(self.runs()[0][-1], "interrupted")
        self.assertEqual(self.agent.conversation.session_id, session)
        self.assertEqual(self.db.one("SELECT status FROM segments")["status"], "closed")
        await asyncio.wait_for(self.brain.idle(), 2)
        self.assertEqual(self.said[-1], "我在")
        self.completer.hold = None
        self.clock.advance(10 * 60)
        await self.sleep.tick()
        self.assertFalse(self.sleep.asleep)  # /wake asks for 30 quiet minutes first
        self.clock.advance(25 * 60)
        await self.sleep.tick()
        self.assertTrue(self.sleep.asleep)
        await self.until_awake()
        self.assertEqual(self.runs()[-1][:2], ("nap", "resume"))
        self.assertNotEqual(self.agent.conversation.session_id, session)
        self.assertIsNone(self.db.get_meta("sleep.resume"))

    async def test_messages_during_sleep_wait_for_waking_up(self):
        await self.chat("先聊一句")
        self.completer.hold = asyncio.Event()
        await self.sleep.request_sleep("manual", kind="nap")
        await asyncio.sleep(0.05)
        self.llm.results.append(reply("早安"))
        self.inbox.put("owner", "早")
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.llm.requests), 1)  # still asleep: the message waits
        self.completer.hold.set()
        await self.until_awake()
        await asyncio.wait_for(self.brain.idle(), 2)
        lines = self.llm.requests[-1]["messages"][1]["content"].splitlines()
        self.assertIn("system: 午睡醒來", lines[0])
        self.assertIn("user: 早", lines[1])

    async def test_interrupted_run_found_at_startup_is_resumed(self):
        self.db.execute("INSERT INTO sleep_runs(kind, reason, started_at, status) VALUES ('night', 'scheduled', ?, 'running')", (self.clock.now(),))
        SleepOrchestrator(
            db=self.db, clock=self.clock, tz=TZ, engine=self.engine, completer=lambda: self.completer, brain=self.brain,
            current_session=lambda: self.agent.conversation.session_id, wake_up=None, notify=None,
        )
        self.assertEqual(self.db.one("SELECT status FROM sleep_runs")["status"], "interrupted")
        self.assertEqual(json.loads(self.db.get_meta("sleep.resume"))["kind"], "night")


if __name__ == "__main__":
    unittest.main()
