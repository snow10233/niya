import asyncio
import unittest

from helpers import TZ, FakeLLM, reply, ts

from niya.agent.context import Conversation
from niya.agent.loop import ITERATION_LIMIT_NOTE, Agent
from niya.clock import FakeClock
from niya.db import Database
from niya.inbox import InboundItem
from niya.tools.registry import Tool, ToolRegistry


def echo_tool(**kwargs):
    async def handler(args, ctx):
        return f"echo {args.get('text', '')}"

    return Tool(name="echo", description="echo", parameters={"type": "object", "properties": {"text": {"type": "string"}}}, handler=handler, **kwargs)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock(ts("2026-10-29 09:15"))
        self.db = Database(":memory:")
        self.conv = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="SYS")

    def agent(self, llm, tools=None, **kwargs):
        return Agent(llm=llm, model="test/model", tools=tools or ToolRegistry(), conversation=self.conv, clock=self.clock, **kwargs)

    def owner(self, text):
        return [InboundItem("owner", text, self.clock.now())]

    async def test_plain_reply(self):
        llm = FakeLLM(reply("[2026-10-29 (四) 09:15] niya: 早安"))
        result = await self.agent(llm).run_turn(self.owner("早"))
        self.assertEqual(result.text, "早安")
        self.assertFalse(result.silent)
        request = llm.requests[0]
        self.assertIsNone(request["tools"])
        self.assertEqual(request["messages"][1], {"role": "user", "content": "[2026-10-29 (四) 09:15] user: 早"})
        turn = self.db.one("SELECT * FROM turns")
        self.assertEqual(turn["source"], "owner")
        self.assertIsNotNone(turn["ended_at"])

    async def test_tool_round_trip_is_persisted(self):
        llm = FakeLLM(reply(None, ("c1", "echo", '{"text": "hi"}')), reply("完成"))
        result = await self.agent(llm, ToolRegistry([echo_tool()])).run_turn(self.owner("試試工具"))
        self.assertEqual(result.text, "完成")
        self.assertEqual(result.iterations, 2)
        second = llm.requests[1]["messages"]
        self.assertEqual(second[-1], {"role": "tool", "tool_call_id": "c1", "content": "echo hi"})
        roles = [r["role"] for r in self.db.query("SELECT role FROM messages ORDER BY id")]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])

    async def test_iteration_limit_forces_summary(self):
        call = ("c", "echo", "{}")
        llm = FakeLLM(
            reply(None, ("c1", *call[1:])),
            reply(None, ("c2", *call[1:])),
            reply("做到一半", ("c3", *call[1:])),
        )
        result = await self.agent(llm, ToolRegistry([echo_tool()]), max_iterations=2).run_turn(self.owner("一直做"))
        self.assertEqual(result.text, "做到一半")
        final = llm.requests[-1]
        self.assertEqual(final["tool_choice"], "none")
        self.assertIn(ITERATION_LIMIT_NOTE, final["messages"][-1]["content"])
        self.assertEqual(self.conv.dangling_tool_calls(), [])

    async def test_silent_reply(self):
        result = await self.agent(FakeLLM(reply("[SILENT] 沒事"))).run_turn(self.owner("x"))
        self.assertTrue(result.silent)

    async def test_cancel_during_tool_closes_dangling_calls(self):
        entered = asyncio.Event()

        async def hang(args, ctx):
            entered.set()
            await asyncio.sleep(60)
            return "never"

        slow = Tool(name="slow", description="", parameters={"type": "object", "properties": {}}, handler=hang)
        agent = self.agent(FakeLLM(reply(None, ("c1", "slow", "{}"))), ToolRegistry([slow]))
        task = asyncio.create_task(agent.run_turn(self.owner("跑久一點")))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.conv.dangling_tool_calls(), [])
        tool_row = self.db.one("SELECT content FROM messages WHERE role = 'tool'")
        self.assertEqual(tool_row["content"], "[已中斷]")

    async def test_history_carries_across_turns_with_timestamps(self):
        llm = FakeLLM(reply("一"), reply("二"))
        agent = self.agent(llm)
        await agent.run_turn(self.owner("第一"))
        self.clock.advance(3600)
        await agent.run_turn(self.owner("第二"))
        contents = [m["content"] for m in llm.requests[1]["messages"][1:]]
        self.assertEqual(
            contents,
            ["[2026-10-29 (四) 09:15] user: 第一", "[2026-10-29 (四) 09:15] niya: 一", "[2026-10-29 (四) 10:15] user: 第二"],
        )
        gap = self.db.query("SELECT gap_s FROM turns ORDER BY started_at")[1]["gap_s"]
        self.assertEqual(gap, 3600)

    async def test_effort_becomes_reasoning_parameter(self):
        llm = FakeLLM(reply("想好了"), reply("再想"))
        agent = self.agent(llm)
        await agent.run_turn(self.owner("想一下"))
        self.assertIsNone(llm.requests[0]["reasoning"])
        await agent.set_effort("high")
        await agent.run_turn(self.owner("再想"))
        self.assertEqual(llm.requests[1]["reasoning"], {"effort": "high"})

    def test_requires_model(self):
        with self.assertRaises(ValueError):
            Agent(llm=FakeLLM(), model="", tools=ToolRegistry(), conversation=self.conv, clock=self.clock)


if __name__ == "__main__":
    unittest.main()
