import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from niya.llm.openrouter import ToolCall
from niya.tools.hardline import default_hardline
from niya.tools.processes import ProcessRegistry
from niya.tools.registry import ToolContext, ToolRegistry
from niya.tools.shell import ShellSession
from niya.tools.terminal import hardline_guard, interpret_exit_code, terminal_tools


class TerminalToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.shell = ShellSession(self.dir / "state", cwd=str(self.dir), env=dict(os.environ))
        self.exited = []
        self.processes = ProcessRegistry(self.dir / "processes", now=asyncio.get_running_loop().time, on_exit=self.exited.append)
        self.registry = ToolRegistry(terminal_tools(self.shell, self.processes, [hardline_guard(default_hardline([self.dir / "data"]))]))
        self.ctx = ToolContext(session_id="s", turn_id="t", now=asyncio.get_running_loop().time)
        self.addAsyncCleanup(self.processes.shutdown)

    async def call(self, name, **args):
        return (await self.registry.execute([ToolCall("c", name, json.dumps(args))], self.ctx))[0]

    async def test_foreground_output_exit_code_and_cwd(self):
        (self.dir / "sub").mkdir()
        out = await self.call("terminal", command="cd sub && echo hi")
        self.assertFalse(out.is_error)
        self.assertIn("hi\n[exit 0]", out.content)
        self.assertIn(f"[目前目錄：{(self.dir / 'sub').resolve()}]", out.content)
        miss = await self.call("terminal", command="grep nothing /dev/null")
        self.assertIn("[exit 1：沒有找到符合的結果（不是錯誤）]", miss.content)

    async def test_hardline_blocks_before_running(self):
        marker = self.dir / "ran"
        out = await self.call("terminal", command=f"touch {marker} && rm -rf /")
        self.assertTrue(out.is_error)
        self.assertIn("黑名單", out.content)
        self.assertFalse(marker.exists())
        own = await self.call("terminal", command=f"rm -rf {self.dir / 'data'}")
        self.assertTrue(own.is_error)

    async def test_timeout_and_bad_workdir(self):
        out = await self.call("terminal", command="sleep 5", timeout=1)
        self.assertIn("[逾時：超過 1 秒", out.content)
        bad = await self.call("terminal", command="ls", workdir="relative")
        self.assertIn("絕對路徑", bad.content)

    async def test_background_process_lifecycle(self):
        out = await self.call("terminal", command="echo one; sleep 0.3; echo two", background=True, notify_on_complete=True)
        self.assertIn("已在背景啟動 p1", out.content)
        listing = await self.call("process", action="list")
        self.assertIn("p1：", listing.content)
        waited = await self.call("process", action="wait", id="p1", timeout=5)
        self.assertIn("已結束（exit 0）", waited.content)
        self.assertIn("one\ntwo", waited.content)
        again = await self.call("process", action="poll", id="p1")
        self.assertIn("（沒有新輸出）", again.content)
        self.assertEqual([p.id for p in self.exited], ["p1"])
        self.assertIn("two", (await self.call("process", action="log", id="p1", lines=1)).content)

    async def test_background_write_and_kill(self):
        await self.call("terminal", command="read line; echo got:$line; sleep 30", background=True)
        await self.call("process", action="write", id="p1", text="hello\n")
        for _ in range(50):
            if "got:hello" in self.processes.get("p1").buffer:
                break
            await asyncio.sleep(0.05)
        self.assertIn("got:hello", self.processes.get("p1").buffer)
        killed = await self.call("process", action="kill", id="p1")
        self.assertIn("已結束", killed.content)
        missing = await self.call("process", action="poll", id="p9")
        self.assertTrue(missing.is_error)

    def test_interpret_exit_code(self):
        self.assertIsNone(interpret_exit_code("ls", 0))
        self.assertEqual(interpret_exit_code("cat x | LC_ALL=C grep foo", 1), "沒有找到符合的結果（不是錯誤）")
        self.assertIn("無法連線", interpret_exit_code("curl http://x", 7))
        self.assertIsNone(interpret_exit_code("make", 2))


if __name__ == "__main__":
    unittest.main()
