import asyncio
import unittest
from types import SimpleNamespace

from helpers import TZ, FakeLLM, reply, ts

from niya.agent.budget import CONTEXT_FULL_NOTE, estimate_tokens
from niya.agent.context import Conversation
from niya.agent.loop import Agent
from niya.brain import Brain
from niya.clock import FakeClock
from niya.db import Database
from niya.inbox import Inbox, InboundItem
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


class SoftLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_soft_line_ends_the_session_and_tells_the_next_turn(self):
        db = Database(":memory:")
        clock = FakeClock(ts("2026-10-29 09:00"))
        conv = Conversation.start_new(db, now=clock.now(), tz=TZ, system_prompt="S")
        llm = FakeLLM(reply("聊完了", prompt_tokens=760), reply("我在", prompt_tokens=100))
        agent = Agent(llm=llm, model="m", tools=ToolRegistry(), conversation=conv, clock=clock, max_context_tokens=1000)
        inbox = Inbox(db, clock)
        events = []

        async def context_full(reason, used):
            events.append((reason, used))
            agent.conversation = Conversation.start_new(db, now=clock.now(), tz=TZ, system_prompt="S")

        async def ignore(text):
            pass

        brain = Brain(inbox=inbox, control=SimpleNamespace(agent=agent), egress=EgressGate(db, now=clock.now, detector=EgressDetector()), reply=ignore, report=ignore, on_context_full=context_full)
        task = asyncio.create_task(brain.run())
        self.addAsyncCleanup(lambda: asyncio.gather(task, return_exceptions=True))
        inbox.put("owner", "很長的對話")
        await asyncio.sleep(0)
        await asyncio.wait_for(brain.idle(), 2)
        self.assertEqual(events, [("context_full", 76)])
        inbox.put("owner", "在嗎")
        await asyncio.sleep(0)
        await asyncio.wait_for(brain.idle(), 2)
        task.cancel()
        first_line = llm.requests[1]["messages"][-1]["content"].splitlines()[0]
        self.assertIn("system: 上一段對話在 2026-10-29 (四) 09:00 因為 context 快滿了（76%）而結束", first_line)
        self.assertEqual(len(llm.requests[1]["messages"]), 2)  # system prompt + the new turn only


if __name__ == "__main__":
    unittest.main()
