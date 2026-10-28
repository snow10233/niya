import asyncio
import unittest

from niya.llm import ToolCall
from niya.tools.registry import Tool, ToolContext, ToolError, ToolRegistry, truncate


def tool(name, handler, **kwargs):
    return Tool(name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler, **kwargs)


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = ToolContext(session_id="s", turn_id="t", now=lambda: 0.0)

    async def test_schemas_sorted_and_cached(self):
        async def noop(args, ctx):
            return ""

        registry = ToolRegistry([tool("b", noop), tool("a", noop)])
        schemas = registry.schemas()
        self.assertEqual([s["function"]["name"] for s in schemas], ["a", "b"])
        self.assertIs(registry.schemas(), schemas)
        with self.assertRaises(ValueError):
            registry.register(tool("a", noop))

    async def test_parallel_safe_calls_run_together_and_keep_order(self):
        started = []
        both_started = asyncio.Event()

        async def search(args, ctx):
            started.append(args["q"])
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return f"result {args['q']}"

        order = []

        async def write(args, ctx):
            order.append("write")
            return "written"

        registry = ToolRegistry([tool("search", search, parallel_safe=True), tool("write", write)])
        calls = [
            ToolCall("1", "search", '{"q": "x"}'),
            ToolCall("2", "search", '{"q": "y"}'),
            ToolCall("3", "write", "{}"),
        ]
        outcomes = await registry.execute(calls, self.ctx)
        self.assertEqual([o.content for o in outcomes], ["result x", "result y", "written"])
        self.assertEqual([o.call.id for o in outcomes], ["1", "2", "3"])

    async def test_errors_become_tool_results(self):
        async def fails(args, ctx):
            raise ToolError("檔案不存在")

        async def crashes(args, ctx):
            raise RuntimeError("boom")

        async def slow(args, ctx):
            await asyncio.sleep(1)
            return "late"

        registry = ToolRegistry([tool("fails", fails), tool("crashes", crashes), tool("slow", slow, timeout_s=0.01)])
        with self.assertLogs("niya.tools.registry", level="ERROR"):
            outcomes = await registry.execute(
                [
                    ToolCall("1", "missing", "{}"),
                    ToolCall("2", "fails", "{bad json"),
                    ToolCall("3", "fails", "[1]"),
                    ToolCall("4", "fails", ""),
                    ToolCall("5", "crashes", "{}"),
                    ToolCall("6", "slow", "{}"),
                ],
                self.ctx,
            )
        self.assertTrue(all(o.is_error for o in outcomes))
        self.assertIn("沒有這個工具", outcomes[0].content)
        self.assertIn("JSON", outcomes[1].content)
        self.assertIn("JSON 物件", outcomes[2].content)
        self.assertEqual(outcomes[3].content, "錯誤：檔案不存在")
        self.assertIn("RuntimeError: boom", outcomes[4].content)
        self.assertIn("逾時", outcomes[5].content)

    async def test_long_output_is_truncated_but_full_text_kept(self):
        async def big(args, ctx):
            return "a" * 600 + "b" * 400

        outcome = (await ToolRegistry([tool("big", big, max_result_chars=100)]).execute([ToolCall("1", "big", "{}")], self.ctx))[0]
        self.assertEqual(len(outcome.full_content), 1000)
        self.assertTrue(outcome.content.startswith("a" * 60))
        self.assertTrue(outcome.content.endswith("b" * 40))
        self.assertIn("省略 900 字元", outcome.content)

    def test_truncate_short_text_unchanged(self):
        self.assertEqual(truncate("abc", 10), "abc")


if __name__ == "__main__":
    unittest.main()
