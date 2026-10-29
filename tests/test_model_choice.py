import tempfile
import unittest
from pathlib import Path

from helpers import TZ, ts

from niya.agent.claude_code import ClaudeCodeAgent
from niya.agent.context import Conversation
from niya.agent.loop import Agent
from niya.clock import FakeClock
from niya.config import Settings, load_settings
from niya.db import Database
from niya.model_choice import ModelChoice, clear_choice, configured_choice, current_choice, parse_choice, save_choice
from niya.runtime import build_agent, build_core_tools, needs_new_session


class ParseTests(unittest.TestCase):
    def test_forms(self):
        cases = {
            "claude:sonnet": ("claude", "sonnet"),
            "sonnet": ("claude", "sonnet"),
            "claude-opus-5-5": ("claude", "claude-opus-5-5"),
            "claude": ("claude", "opus"),
            "claude:": ("claude", "opus"),
            "anthropic/claude-sonnet-4.6": ("openrouter", "anthropic/claude-sonnet-4.6"),
            "openrouter:meta-llama/llama-4:free": ("openrouter", "meta-llama/llama-4:free"),
            "vendor/model:free": ("openrouter", "vendor/model:free"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_choice(text), ModelChoice(*expected))
        with self.assertRaises(ValueError):
            parse_choice("openrouter")

    def test_configured_defaults(self):
        home = Path("/tmp/x")
        self.assertEqual(configured_choice(Settings(home=home)), ModelChoice("claude", "opus"))
        self.assertEqual(configured_choice(Settings(home=home, model="vendor/m")), ModelChoice("openrouter", "vendor/m"))
        self.assertEqual(configured_choice(Settings(home=home, provider="claude", model="haiku")), ModelChoice("claude", "haiku"))

    def test_settings_validate_provider_and_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "niya.toml"
            config.write_text('[agent]\nprovider = "claude"\neffort = "high"\n')
            settings = load_settings(config, environ={"NIYA_HOME": tmp, "CLAUDE_CODE_OAUTH_TOKEN": "tok-123", "ANTHROPIC_API_KEY": ""})
            self.assertEqual((settings.provider, settings.effort), ("claude", "high"))
            self.assertEqual(settings.secrets, {"CLAUDE_CODE_OAUTH_TOKEN": "tok-123"})
            with self.assertRaises(ValueError):
                load_settings(config, environ={"NIYA_HOME": tmp, "NIYA_PROVIDER": "gemini"})
            relative = load_settings(config, environ={"NIYA_HOME": ".niya-test"})
            self.assertEqual(relative.home, Path.cwd() / ".niya-test")
            config.write_text('[agent]\neffort = "turbo"\n')
            with self.assertRaises(ValueError):
                load_settings(config, environ={"NIYA_HOME": tmp})


class ChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = Settings(home=self.dir, model="vendor/m", secrets={"OPENROUTER_API_KEY": "sk-or-v1-key000000000"})
        self.db = Database(":memory:")
        self.clock = FakeClock(ts("2026-10-29 10:00"))

    def test_saved_choice_wins_until_cleared(self):
        self.assertEqual(current_choice(self.db, self.settings), (ModelChoice("openrouter", "vendor/m"), "設定檔"))
        save_choice(self.db, ModelChoice("claude", "sonnet"))
        self.assertEqual(current_choice(self.db, self.settings), (ModelChoice("claude", "sonnet"), "手動選擇"))
        clear_choice(self.db)
        self.assertEqual(current_choice(self.db, self.settings)[1], "設定檔")

    async def test_build_agent_picks_engine_and_switching_provider_needs_new_session(self):
        tools = build_core_tools(self.settings, self.db, self.clock)
        self.addAsyncCleanup(tools.close)
        conv = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S")
        claude = build_agent(self.settings, self.db, self.clock, tools, conv, ModelChoice("claude", "opus"))
        self.assertIsInstance(claude, ClaudeCodeAgent)
        self.assertEqual(claude.options().cwd, str(self.dir))
        openrouter = build_agent(self.settings, self.db, self.clock, tools, conv, ModelChoice("openrouter", "vendor/m"))
        self.addAsyncCleanup(openrouter.aclose)
        self.assertIsInstance(openrouter, Agent)
        self.assertFalse(needs_new_session(conv, ModelChoice("claude", "opus")))
        conv.add_system_line(None, "x", ts=self.clock.now())
        conv.bind_engine("openrouter")
        self.assertTrue(needs_new_session(conv, ModelChoice("claude", "opus")))
        self.assertFalse(needs_new_session(conv, ModelChoice("openrouter", "other/m")))
        with self.assertRaises(ValueError):
            build_agent(Settings(home=self.dir), self.db, self.clock, tools, conv, ModelChoice("openrouter", "vendor/m"))


if __name__ == "__main__":
    unittest.main()
