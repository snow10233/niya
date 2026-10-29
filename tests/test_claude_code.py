import asyncio
import tempfile
import unittest
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from helpers import TZ, ts

from niya.agent.claude_code import CAP_TOOL_TEXT, ClaudeCodeAgent
from niya.agent.context import Conversation
from niya.clock import FakeClock
from niya.config import Settings
from niya.db import Database
from niya.inbox import InboundItem
from niya.llm import LLMError, db_recorder
from niya.runtime import build_core_tools

SID = "11111111-2222-3333-4444-555555555555"
USAGE = {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 5, "output_tokens": 20}


def say(text, message_id="m-text"):
    return AssistantMessage(content=[TextBlock(text)], model="claude-opus-5-5", message_id=message_id, session_id=SID, usage=USAGE)


def done(**kwargs):
    fields = dict(subtype="success", duration_ms=5, duration_api_ms=4, is_error=False, num_turns=1, session_id=SID, total_cost_usd=0.01, usage=USAGE)
    return ResultMessage(**{**fields, **kwargs})


def tool_round(message_id, *calls):
    """Claude asks for tools, the fake CLI runs niya's MCP handlers, then streams the results back."""

    async def step(client):
        blocks = [ToolUseBlock(id=i, name=f"mcp__niya__{name}", input=args) for i, name, args in calls]
        yield AssistantMessage(content=blocks, model="claude-opus-5-5", message_id=message_id, session_id=SID, usage=USAGE)
        tools = {t.name: t for t in client.harness.agent.sdk_tools}
        for call_id, name, args in calls:
            result = await tools[name].handler(args)
            yield UserMessage(content=[ToolResultBlock(tool_use_id=call_id, content=result["content"], is_error=result["is_error"])])

    return step


class FakeClient:
    def __init__(self, options, harness):
        self.options = options
        self.harness = harness
        self.queries = []
        self.models = []
        self.disconnected = False

    async def connect(self):
        if self.harness.fail_connect:
            self.harness.fail_connect = False
            raise CLIConnectionError("boom")
        if self.options.resume in self.harness.missing_sessions:
            raise ProcessError(f"Claude Code returned an error result: No conversation found with session ID: {self.options.resume}", exit_code=1)

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        for step in self.harness.script.pop(0):
            if callable(step):
                async for message in step(self):
                    yield message
            else:
                yield step

    async def set_model(self, model=None):
        self.models.append(model)

    async def disconnect(self):
        self.disconnected = True


class Harness:
    def __init__(self):
        self.script = []
        self.clients = []
        self.agent = None
        self.fail_connect = False
        self.missing_sessions = set()

    def factory(self, options):
        client = FakeClient(options, self)
        self.clients.append(client)
        return client


class ClaudeCodeAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        secrets = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-subscriptiontoken", "OPENROUTER_API_KEY": "sk-or-v1-supersecretvalue0001"}
        self.settings = Settings(home=self.dir / "home", secrets=secrets)
        self.clock = FakeClock(ts("2026-10-29 10:00"))
        self.db = Database(":memory:")
        self.tools = build_core_tools(self.settings, self.db, self.clock)
        self.addAsyncCleanup(self.tools.close)
        self.conv = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S")
        self.harness = Harness()

    def agent(self, *turns, conversation=None, max_iterations=40):
        self.harness.script.extend(list(t) for t in turns)
        agent = ClaudeCodeAgent(
            model="opus",
            tools=self.tools.registry,
            conversation=conversation or self.conv,
            clock=self.clock,
            config_dir=self.settings.claude_config_dir,
            cwd=self.settings.home,
            max_iterations=max_iterations,
            secrets=self.settings.secrets,
            recorder=db_recorder(self.db, self.clock),
            client_factory=self.harness.factory,
        )
        self.harness.agent = agent
        return agent

    def owner(self, text):
        return [InboundItem("owner", text, self.clock.now())]

    def rows(self):
        return [(r["role"], r["tool_name"], r["content"]) for r in self.db.query("SELECT * FROM messages ORDER BY id")]

    async def test_text_reply_options_and_bookkeeping(self):
        agent = self.agent([SystemMessage("init", {"session_id": SID}), say("[2026-10-29 (四) 10:00] niya: 早安！"), done()])
        result = await agent.run_turn(self.owner("早"))
        self.assertEqual(result.text, "早安！")
        self.assertEqual(result.prompt_tokens, 1015)
        client = self.harness.clients[0]
        self.assertEqual(client.queries, ["[2026-10-29 (四) 10:00] user: 早"])
        options = client.options
        self.assertEqual(options.system_prompt, "S")
        self.assertEqual(options.tools, [])
        self.assertIn("mcp__niya__terminal", options.allowed_tools)
        self.assertEqual(options.permission_mode, "dontAsk")
        self.assertEqual(options.setting_sources, [])
        self.assertTrue(options.verbatim_prompts)
        self.assertIsNone(options.resume)
        self.assertEqual(options.env["CLAUDE_CONFIG_DIR"], str(self.settings.claude_config_dir))
        self.assertEqual(options.env["DISABLE_AUTO_COMPACT"], "1")
        self.assertEqual(options.env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-subscriptiontoken")
        self.assertNotIn("OPENROUTER_API_KEY", options.env)
        self.assertEqual((self.conv.engine, self.conv.engine_session_id), ("claude", SID))
        self.assertEqual(self.db.one("SELECT engine_session_id FROM sessions")["engine_session_id"], SID)
        call = self.db.one("SELECT * FROM llm_calls")
        self.assertEqual((call["model"], call["prompt_tokens"], call["ok"]), ("claude:opus", 1015, 1))
        self.assertIsNotNone(self.db.one("SELECT ended_at FROM turns")["ended_at"])

    async def test_tools_run_through_registry_and_full_output_is_kept(self):
        secret_file = self.dir / "leak.txt"
        secret_file.write_text("key=sk-or-v1-supersecretvalue0001\n")
        big = "python3 -c \"print('x' * 40000)\""
        agent = self.agent([
            tool_round("m1", ("toolu_1", "terminal", {"command": f"cat {secret_file}"}), ("toolu_2", "terminal", {"command": big})),
            say("看完了", "m2"),
            done(),
        ])
        result = await agent.run_turn(self.owner("看一下"))
        self.assertEqual(result.text, "看完了")
        self.assertEqual(result.iterations, 1)
        self.assertEqual([r[:2] for r in self.rows()], [("user", None), ("assistant", None), ("tool", "terminal"), ("tool", "terminal"), ("assistant", None)])
        assistant = self.conv.messages[1]
        self.assertEqual([c["id"] for c in assistant.tool_calls], ["toolu_1", "toolu_2"])
        self.assertEqual(assistant.tool_calls[0]["function"]["name"], "terminal")
        leak, long = self.conv.messages[2], self.conv.messages[3]
        self.assertNotIn("supersecretvalue0001", leak.content + leak.api_content)
        self.assertEqual(long.tool_call_id, "toolu_2")
        self.assertGreater(len(long.content), 40000)
        self.assertLess(len(long.api_content), 32000)

    async def test_split_stream_messages_merge_into_one_row(self):
        split = [
            AssistantMessage(content=[TextBlock("我看看")], model="m", message_id="m1", session_id=SID),
            AssistantMessage(content=[ToolUseBlock(id="toolu_1", name="mcp__niya__terminal", input={"command": "true"})], model="m", message_id="m1", session_id=SID),
        ]

        async def step(client):
            for message in split:
                yield message
            result = await {t.name: t for t in client.harness.agent.sdk_tools}["terminal"].handler({"command": "true"})
            yield UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content=result["content"])])

        agent = self.agent([step, say("好了", "m2"), done()])
        await agent.run_turn(self.owner("跑一下"))
        assistant = self.conv.messages[1]
        self.assertEqual((assistant.content, len(assistant.tool_calls)), ("我看看", 1))

    async def test_resume_after_restart_and_new_session_after_sleep(self):
        await self.agent([say("一"), done()]).run_turn(self.owner("1"))
        restarted = self.agent([say("二"), done()])
        await restarted.run_turn(self.owner("2"))
        self.assertEqual(self.harness.clients[1].options.resume, SID)
        restarted.conversation = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S2")
        self.harness.script.append([say("三"), done(session_id="new-session")])
        await restarted.run_turn(self.owner("3"))
        self.assertTrue(self.harness.clients[1].disconnected)
        self.assertIsNone(self.harness.clients[2].options.resume)
        self.assertEqual(self.harness.clients[2].options.system_prompt, "S2")
        self.assertEqual(restarted.conversation.engine_session_id, "new-session")

    async def test_missing_transcript_starts_fresh_and_says_so(self):
        self.conv.bind_engine("claude", "gone-session")
        self.harness.missing_sessions.add("gone-session")
        agent = self.agent([say("好，重新開始"), done(session_id="fresh-session")])
        result = await agent.run_turn(self.owner("還記得嗎"))
        self.assertEqual(result.text, "好，重新開始")
        self.assertEqual([c.options.resume for c in self.harness.clients], ["gone-session", None])
        prompt = self.harness.clients[1].queries[0]
        self.assertTrue(prompt.startswith("[2026-10-29 (四) 10:00] user: 還記得嗎\n[2026-10-29 (四) 10:00] system: 這個 session 先前的對話接續不上了"))
        self.assertEqual(self.conv.engine_session_id, "fresh-session")

    async def test_relative_paths_reach_claude_code_as_absolute(self):
        agent = ClaudeCodeAgent(
            model="opus", tools=self.tools.registry, conversation=self.conv, clock=self.clock,
            config_dir=Path("rel/claude"), cwd=Path("rel"), client_factory=self.harness.factory,
        )
        options = agent.options()
        self.assertTrue(Path(options.cwd).is_absolute())
        self.assertTrue(Path(options.env["CLAUDE_CONFIG_DIR"]).is_absolute())

    async def test_iteration_cap_refuses_more_tools(self):
        agent = self.agent(
            [
                tool_round("m1", ("toolu_1", "terminal", {"command": "echo one"})),
                tool_round("m2", ("toolu_2", "terminal", {"command": "echo two"})),
                say("先停在這", "m3"),
                done(),
            ],
            max_iterations=1,
        )
        result = await agent.run_turn(self.owner("一直跑"))
        tool_rows = [m for m in self.conv.messages if m.role == "tool"]
        self.assertIn("one", tool_rows[0].content)
        self.assertEqual(tool_rows[1].content, CAP_TOOL_TEXT)
        self.assertEqual(result.text, "先停在這")

    async def test_rate_limit_becomes_readable_error(self):
        info = RateLimitInfo(status="rejected", resets_at=int(ts("2026-10-29 15:00")), rate_limit_type="five_hour")
        error = AssistantMessage(content=[TextBlock("API Error: 429")], model="m", error="rate_limit", session_id=SID)
        agent = self.agent([RateLimitEvent(rate_limit_info=info, uuid="u", session_id=SID), error, done(is_error=True, api_error_status=429)])
        with self.assertRaises(LLMError) as caught:
            await agent.run_turn(self.owner("在嗎"))
        self.assertIn("5 小時額度", str(caught.exception))
        self.assertIn("10/29 15:00", str(caught.exception))
        self.assertEqual([r[0] for r in self.rows()], ["user"])
        self.assertIsNotNone(self.db.one("SELECT ended_at FROM turns")["ended_at"])
        self.assertEqual(self.db.one("SELECT error FROM llm_calls")["error"], "api_error 429")

    async def test_cancel_closes_dangling_calls_and_drops_client(self):
        started = asyncio.Event()

        async def hang(client):
            yield AssistantMessage(content=[ToolUseBlock(id="toolu_1", name="mcp__niya__terminal", input={"command": "sleep 5"})], model="m", message_id="m1", session_id=SID)
            started.set()
            await asyncio.Event().wait()

        agent = self.agent([hang])
        task = asyncio.create_task(agent.run_turn(self.owner("跑很久的")))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.rows()[-1], ("tool", "terminal", "[已中斷]"))
        self.assertTrue(self.harness.clients[0].disconnected)

    async def test_connection_failure_is_an_llm_error_and_next_turn_reconnects(self):
        agent = self.agent([say("回來了"), done()])
        self.harness.fail_connect = True
        with self.assertRaises(LLMError):
            await agent.run_turn(self.owner("1"))
        result = await agent.run_turn(self.owner("2"))
        self.assertEqual(result.text, "回來了")

    async def test_effort_change_reconnects_and_resumes(self):
        agent = self.agent([say("一"), done()], [say("二"), done()])
        await agent.run_turn(self.owner("1"))
        self.assertIsNone(self.harness.clients[0].options.effort)
        await agent.set_effort("max")
        self.assertTrue(self.harness.clients[0].disconnected)
        await agent.run_turn(self.owner("2"))
        self.assertEqual((self.harness.clients[1].options.effort, self.harness.clients[1].options.resume), ("max", SID))
        await agent.set_effort("minimal")
        self.assertEqual(agent.effort, "low")

    async def test_set_model_reaches_live_client(self):
        agent = self.agent([say("好"), done()])
        await agent.run_turn(self.owner("1"))
        await agent.set_model("sonnet")
        self.assertEqual(self.harness.clients[0].models, ["sonnet"])
        self.assertEqual(agent.options().model, "sonnet")


if __name__ == "__main__":
    unittest.main()
