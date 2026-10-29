import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import TZ

from niya.core.media import MediaStore
from niya.llm.openrouter import ToolCall
from niya.tools import browser
from niya.tools.browser import browser_tools, find_cli
from niya.tools.registry import ToolContext, ToolRegistry

# Stands in for the browser-use CLI: runs the piped code as plain Python.
FAKE_CLI = [sys.executable, "-c", "import sys; exec(sys.stdin.read())"]


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.dir / "workspace"
        env = {"PATH": "/usr/bin:/bin", "OPENROUTER_API_KEY": "sk-or-v1-secret"}
        tools = browser_tools(
            FAKE_CLI, workspace=self.workspace, media=MediaStore(self.dir / "media", TZ), env=env,
            secret_names=["OPENROUTER_API_KEY"], secrets=["sk-or-v1-secret"],
        )
        self.registry = ToolRegistry(tools)

    async def run_code(self, code, **extra):
        ctx = ToolContext("s", "t", now=lambda: 0.0)
        [outcome] = await self.registry.execute([ToolCall("c1", "browser_exec", json.dumps({"code": code, **extra}))], ctx)
        return outcome

    async def test_output_workspace_and_no_secrets(self):
        outcome = await self.run_code("# 看環境\nimport os\nprint(os.environ['BH_AGENT_WORKSPACE'], os.environ.get('OPENROUTER_API_KEY'))")
        self.assertFalse(outcome.is_error)
        self.assertIn(f"exit 0，workspace：{self.workspace}", outcome.content)
        self.assertIn(f"{self.workspace} None", outcome.content)
        self.assertEqual(outcome.external_source, "browser")

    async def test_screenshot_is_copied_and_attached(self):
        shot = self.dir / "shot.png"
        code = f"from PIL import Image\nImage.new('RGB', (4, 4)).save({str(shot)!r})\nprint('saved', {str(shot)!r})"
        outcome = await self.run_code(code)
        [copy] = outcome.images
        self.assertTrue(copy.startswith(str(self.dir / "media" / "screenshots")))
        self.assertEqual(Path(copy).read_bytes(), shot.read_bytes())
        self.assertIn(f"截圖：{copy}", outcome.content)
        os.utime(shot, (1, 1))
        self.assertEqual((await self.run_code(f"print({str(shot)!r})")).images, (), "an old screenshot is not this run's")

    async def test_unsafe_urls_never_reach_the_browser(self):
        self.assertIn("不能開", (await self.run_code("new_tab('http://127.0.0.1:8080/admin')")).content)
        self.assertIn("密鑰", (await self.run_code("new_tab('https://example.com/?k=sk-or-v1-secret')")).content)

    async def test_timeout_stops_the_run(self):
        browser.MIN_TIMEOUT_S, saved = 1, browser.MIN_TIMEOUT_S
        self.addCleanup(setattr, browser, "MIN_TIMEOUT_S", saved)
        outcome = await self.run_code("import time\ntime.sleep(30)", timeout_s=1)
        self.assertTrue(outcome.is_error)
        self.assertIn("超過 1 秒", outcome.content)

    def test_find_cli(self):
        self.assertEqual(find_cli("uvx browser-use"), ["uvx", "browser-use"])
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        cli = bin_dir / "browser-use"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        self.assertEqual(find_cli(path=str(bin_dir)), [str(cli)])


if __name__ == "__main__":
    unittest.main()
