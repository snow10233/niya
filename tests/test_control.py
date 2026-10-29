import tempfile
import unittest
from pathlib import Path

import httpx
from helpers import TZ, ts

from niya.agent.claude_code import ClaudeCodeAgent
from niya.agent.context import Conversation
from niya.agent.loop import Agent
from niya.clock import FakeClock
from niya.config import Settings
from niya.control import ModelControl
from niya.db import Database
from niya.llm import ModelInfo
from niya.model_choice import ModelChoice, current_choice, current_effort, search_models
from niya.runtime import build_agent, build_core_tools

CATALOG = [
    ModelInfo("anthropic/claude-sonnet-5.5", "Anthropic: Claude Sonnet 5.5", 1_000_000, 2.0, 10.0, True),
    ModelInfo("anthropic/claude-sonnet-5.5:batch", "Anthropic: Claude Sonnet 5.5 (batch)", 1_000_000, 1.0, 5.0, True),
    ModelInfo("google/gemini-3.8-flash", "Google: Gemini 3.8 Flash", 1_048_576, 0.8, 4.1, True),
    ModelInfo("vendor/no-tools", "Vendor: Sonnet-ish chat", 8192, None, None, False),
]


class SearchTests(unittest.TestCase):
    def test_search_keeps_tool_capable_models_matching_every_word(self):
        self.assertEqual([m.id for m in search_models(CATALOG, "sonnet")], ["anthropic/claude-sonnet-5.5"])
        self.assertEqual([m.id for m in search_models(CATALOG, "GEMINI flash")], ["google/gemini-3.8-flash"])
        self.assertEqual(search_models(CATALOG, "gemini pro"), [])


class ModelControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = Settings(home=self.dir, secrets={"OPENROUTER_API_KEY": "sk-or-v1-key000000000"})
        self.db = Database(":memory:")
        self.clock = FakeClock(ts("2026-10-29 10:00"))
        self.tools = build_core_tools(self.settings, self.db, self.clock)
        self.addAsyncCleanup(self.tools.close)
        self.conv = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S")
        self.answers = []
        self.catalog_calls = 0

        async def confirm(question):
            self.answers.append(question)
            return self.answer

        async def catalog():
            self.catalog_calls += 1
            return CATALOG

        self.answer = True
        agent = build_agent(self.settings, self.db, self.clock, self.tools, self.conv, ModelChoice("claude", "opus"))
        self.control = ModelControl(
            settings=self.settings, db=self.db, clock=self.clock, tools=self.tools, prompt=lambda: "S2", agent=agent, confirm=confirm, catalog=catalog
        )
        async def close_agent():
            await self.control.agent.aclose()

        self.addAsyncCleanup(close_agent)

    async def test_search_then_pick_by_number(self):
        listing = await self.control.model("search sonnet")
        self.assertIn("1. anthropic/claude-sonnet-5.5", listing)
        self.assertNotIn(":batch", listing)
        reply = await self.control.model("1")
        self.assertIn("openrouter:anthropic/claude-sonnet-5.5", reply)
        self.assertIsInstance(self.control.agent, Agent)
        self.assertEqual(self.control.agent.model, "anthropic/claude-sonnet-5.5")
        self.assertEqual(current_choice(self.db, self.settings)[0], ModelChoice("openrouter", "anthropic/claude-sonnet-5.5"))
        self.assertEqual(self.answers, [])  # empty session: nothing to lose, no question
        self.assertIn("沒有這個編號", await self.control.model("9"))
        self.assertEqual(self.catalog_calls, 1)

    async def test_typed_openrouter_ids_are_checked(self):
        self.assertIn("找不到", await self.control.model("openrouter:anthropic/claude-sonnet-9"))
        self.assertIn("不支援工具呼叫", await self.control.model("vendor/no-tools"))
        self.assertIsInstance(self.control.agent, ClaudeCodeAgent)
        self.assertIn("已切換", await self.control.model("google/gemini-3.8-flash"))

    async def test_catalog_unreachable_still_allows_typed_ids(self):
        async def offline():
            raise httpx.ConnectError("down")

        self.control.catalog = offline
        self.assertIn("連不上", await self.control.model("search sonnet"))
        self.assertIn("已切換", await self.control.model("openrouter:some/model"))

    async def test_switching_provider_with_history_asks_first(self):
        self.conv.add_system_line(None, "x", ts=self.clock.now())
        self.conv.bind_engine("claude")
        self.answer = False
        self.assertEqual(await self.control.model("google/gemini-3.8-flash"), "沒有切換。")
        self.assertIsInstance(self.control.agent, ClaudeCodeAgent)
        self.assertEqual(current_choice(self.db, self.settings)[1], "設定檔")
        self.answer = True
        await self.control.model("google/gemini-3.8-flash")
        self.assertEqual(len(self.answers), 2)
        self.assertNotEqual(self.control.agent.conversation.session_id, self.conv.session_id)
        self.assertEqual(self.db.one("SELECT end_reason FROM sessions WHERE id = ?", (self.conv.session_id,))["end_reason"], "engine_switch")

    async def test_same_provider_switch_keeps_session_and_reset_returns_to_config(self):
        agent = self.control.agent
        await self.control.model("sonnet")
        self.assertIs(self.control.agent, agent)
        self.assertEqual(agent.model, "sonnet")
        await self.control.model("reset")
        self.assertEqual(agent.model, "opus")
        self.assertEqual(current_choice(self.db, self.settings)[1], "設定檔")

    async def test_no_key_means_no_switch(self):
        self.control.settings = Settings(home=self.dir)
        self.assertIn("OPENROUTER_API_KEY", await self.control.model("google/gemini-3.8-flash"))
        self.assertIsInstance(self.control.agent, ClaudeCodeAgent)

    async def test_effort_levels_follow_the_engine(self):
        self.assertIn("預設", await self.control.effort(""))
        self.assertIn("沒有「minimal」", await self.control.effort("minimal"))
        self.assertIn("max", await self.control.effort("max"))
        self.assertEqual(self.control.agent.effort, "max")
        self.assertEqual(current_effort(self.db, self.settings), ("max", "手動選擇"))
        await self.control.model("google/gemini-3.8-flash")
        self.assertEqual(self.control.agent.effort, "max")
        await self.control.effort("minimal")
        await self.control.model("claude")
        self.assertEqual(self.control.agent.effort, "low")
        self.assertIn("claude 不支援 minimal", self.control.status())
        await self.control.effort("reset")
        self.assertIsNone(self.control.agent.effort)


if __name__ == "__main__":
    unittest.main()
