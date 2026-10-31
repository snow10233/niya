import asyncio
import unittest
from types import SimpleNamespace

from helpers import TZ, FakeLLM, reply, ts

from niya.agent.budget import CONTEXT_FULL_NOTE, estimate_tokens
from niya.agent.compact import CONTINUE_NOTE, HANDOFF_REQUEST, MAX_CONTINUATIONS, STOPPED_NOTE
from niya.agent.context import Conversation
from niya.agent.loop import Agent
from niya.app.brain import Brain
from niya.core.clock import FakeClock
from niya.core.db import Database
from niya.core.inbox import InboundItem, Inbox
from niya.tools.egress import EgressDetector, EgressGate
from niya.tools.registry import Tool, ToolRegistry


def big_tool(chars):
    async def handler(args, ctx):
        return "字" * chars

    return Tool(name="dump", description="d", parameters={"type": "object", "properties": {}}, handler=handler)


class EstimateTests(unittest.TestCase):
    def test_estimates_are_generous(self):
        self.assertEqual(estimate_tokens("你好"), 2)
        self.assertEqual(estimate_tokens("hello world"), 4)


class HardLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock(ts("2026-10-29 09:00"))
        self.db = Database(":memory:")
        self.conv = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S")

    def agent(self, llm, chars=10):
        return Agent(llm=llm, model="m", tools=ToolRegistry([big_tool(chars)]), conversation=self.conv, clock=self.clock, max_context_tokens=1000)

    async def test_large_tool_output_pushes_over_the_hard_line_and_the_turn_wraps_up(self):
        llm = FakeLLM(reply(None, ("c1", "dump", "{}"), prompt_tokens=800), reply("目前做到一半", prompt_tokens=1010))
        result = await self.agent(llm, chars=150).run_turn([InboundItem("owner", "跑", self.clock.now())])
        self.assertTrue(result.context_full)
        self.assertEqual(llm.requests[1]["tool_choice"], "none")
        self.assertIn(CONTEXT_FULL_NOTE, llm.requests[1]["messages"][-1]["content"])

    async def test_under_the_line_keeps_going_and_new_session_resets(self):
        llm = FakeLLM(reply(None, ("c1", "dump", "{}"), prompt_tokens=800), reply("好了", prompt_tokens=820))

        async def context_length(model):
            return 4000

        llm.context_length = context_length
        agent = self.agent(llm)
        result = await agent.run_turn([InboundItem("owner", "跑", self.clock.now())])
        self.assertFalse(result.context_full)
        self.assertEqual(agent.context_limit, 1000)  # the configured cap is smaller than the model window
        agent.conversation = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S")
        self.assertEqual(agent.context_tokens(), 0)


class CompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.clock = FakeClock(ts("2026-10-29 09:00"))
        self.conv = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S")
        self.inbox = Inbox(self.db, self.clock)
        self.reports = []
        self.fallbacks = []

    def brain(self, llm, tools=None):
        self.agent = Agent(llm=llm, model="m", tools=tools or ToolRegistry(), conversation=self.conv, clock=self.clock, max_context_tokens=1000)

        async def fallback(used, progress):
            self.fallbacks.append((used, progress))

        async def report(text):
            self.reports.append(text)

        async def ignore(text):
            pass

        brain = Brain(
            inbox=self.inbox,
            control=SimpleNamespace(agent=self.agent),
            egress=EgressGate(self.db, now=self.clock.now, detector=EgressDetector()),
            reply=ignore,
            report=report,
            on_compact_failed=fallback,
        )
        task = asyncio.create_task(brain.run())
        self.addAsyncCleanup(lambda: asyncio.gather(task, return_exceptions=True))
        self.addCleanup(task.cancel)
        return brain

    async def say(self, brain, text):
        self.inbox.put("owner", text)
        await asyncio.sleep(0)
        await asyncio.wait_for(brain.idle(), 2)

    async def test_soft_line_compacts_and_the_session_goes_on_from_the_handoff(self):
        llm = FakeLLM(reply("聊完了", prompt_tokens=760), reply("交接：主人在聊京都行程，還沒訂車票", prompt_tokens=780), reply("我在", prompt_tokens=300))
        brain = self.brain(llm)
        await self.say(brain, "很長的對話")
        request = llm.requests[1]
        self.assertEqual((request["task"], request["messages"][-1]["content"]), ("compact", HANDOFF_REQUEST))
        self.assertEqual(request["messages"][:-1], llm.requests[0]["messages"] + [{"role": "assistant", "content": "[2026-10-29 (四) 09:00] niya: 聊完了"}])
        self.assertEqual(self.reports, ["（context 用了 76%，前面的對話換成交接摘要了）"])
        self.assertEqual(self.fallbacks, [])
        await self.say(brain, "在嗎")
        messages = llm.requests[2]["messages"]
        self.assertEqual(len(messages), 2)  # system prompt + the handoff riding on the new turn
        text = messages[1]["content"]
        self.assertTrue(text.startswith("<context_handoff>"))
        self.assertIn("# 交接摘要\n交接：主人在聊京都行程，還沒訂車票", text)
        self.assertIn("[2026-10-29 (四) 09:00] user: 很長的對話\n[2026-10-29 (四) 09:00] niya: 聊完了", text)
        self.assertTrue(text.endswith("</context_handoff>\n\n[2026-10-29 (四) 09:00] user: 在嗎"))
        # Nothing is lost from the record, and a reopened session renders the same way.
        self.assertEqual([r["role"] for r in self.db.query("SELECT role FROM messages ORDER BY id")], ["user", "assistant", "user", "assistant"])
        self.assertEqual(Conversation(self.db, self.conv.session_id, TZ).api_messages(), messages + [{"role": "assistant", "content": "[2026-10-29 (四) 09:00] niya: 我在"}])

    @staticmethod
    def cut():
        """A turn whose tool output overflows the context, then the handoff it writes."""
        return [reply(None, ("c1", "dump", "{}"), prompt_tokens=800), reply("做到一半，下一步改設定", prompt_tokens=1010), reply("交接：下一步改設定", prompt_tokens=1020)]

    async def test_cut_short_turn_carries_on_by_itself(self):
        llm = FakeLLM(*self.cut(), reply("改好了", prompt_tokens=300))
        brain = self.brain(llm, ToolRegistry([big_tool(150)]))
        await self.say(brain, "幫我把設定改完")
        await asyncio.wait_for(brain.idle(), 2)
        self.assertEqual(len(llm.requests), 4)
        last_turn = llm.requests[3]["messages"][1]["content"]
        self.assertIn("交接：下一步改設定", last_turn)
        self.assertTrue(last_turn.endswith(f"system: {CONTINUE_NOTE}"))
        self.assertEqual(brain.continued, 1)

    async def test_continuations_stop_at_the_cap_until_the_owner_speaks(self):
        llm = FakeLLM(*[r for _ in range(MAX_CONTINUATIONS + 1) for r in self.cut()], reply("好", prompt_tokens=300))
        brain = self.brain(llm, ToolRegistry([big_tool(150)]))
        await self.say(brain, "幫我把設定改完")
        await asyncio.wait_for(brain.idle(), 2)
        self.assertEqual(len(llm.requests), 3 * (MAX_CONTINUATIONS + 1))
        self.assertEqual(self.reports[-1], STOPPED_NOTE)
        self.assertEqual(self.inbox.pending(), [])
        await self.say(brain, "繼續")
        self.assertEqual(brain.continued, 0)

    async def test_failed_compaction_falls_back_to_an_early_sleep(self):
        llm = FakeLLM(reply("聊完了", prompt_tokens=760), reply("", prompt_tokens=780))
        brain = self.brain(llm)
        await self.say(brain, "很長的對話")
        self.assertEqual(self.fallbacks, [(76, "")])
        self.assertIsNone(self.db.one("SELECT id FROM compactions"))


if __name__ == "__main__":
    unittest.main()
