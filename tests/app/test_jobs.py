import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import httpx
from helpers import TZ, ts

from niya.app.jobs import child_env, default_cwd, parse_target, report_of, run_job

SECRET = "sk-discord-secret-0123456789"


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.posts = []

        def discord(request):
            self.posts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"id": "1"})

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(discord))
        self.addAsyncCleanup(self.http.aclose)

    async def job(self, code, *, deliver="10", on_failure="20", timeout_s=30.0, token="bot-token"):
        script = self.dir / "job.py"
        script.write_text(code)
        out = StringIO()
        with redirect_stdout(out):
            code = await run_job(
                name="snowbox 健康監控", command=[sys.executable, str(script)], deliver=deliver, on_failure=on_failure, cwd=None,
                env={"PATH": os.environ["PATH"], "TOKEN": SECRET}, timeout_s=timeout_s, token=token,
                redact=lambda text: text.replace(SECRET, "[REDACTED]"), tz=TZ, now=lambda: ts("2026-10-30 15:05"), http=self.http,
            )
        return code, out.getvalue()

    async def test_output_goes_to_discord_with_secrets_masked(self):
        code, _ = await self.job("import os\nprint('記憶體 92%')\nprint('token', os.environ['TOKEN'])\nprint('x' * 2500)")
        self.assertEqual(code, 0)
        self.assertGreater(len(self.posts), 1)  # long output is split
        self.assertEqual({path for path, _ in self.posts}, {"/api/v10/channels/10/messages"})
        first = self.posts[0][1]
        self.assertTrue(first["content"].startswith("記憶體 92%\ntoken [REDACTED]"))
        self.assertEqual(first["allowed_mentions"], {"parse": []})

    async def test_nothing_to_say_says_nothing(self):
        for code in ("pass", "print('   ')", "print('checked'); print('{\"wakeAgent\": false}')"):
            with self.subTest(code=code):
                self.assertEqual((await self.job(code))[0], 0)
        self.assertEqual(self.posts, [])
        self.assertIsNone(report_of('{"wakeAgent": false}'))
        self.assertEqual(report_of('{"wakeAgent": true}'), '{"wakeAgent": true}')

    async def test_failures_are_reported_where_failures_go(self):
        code, _ = await self.job("import sys\nprint('half done')\nsys.exit('disk full')")
        self.assertEqual(code, 1)
        [(path, body)] = self.posts
        self.assertEqual(path, "/api/v10/channels/20/messages")
        self.assertIn("⚠️ 排程「snowbox 健康監控」的腳本失敗了", body["content"])
        self.assertIn("exit 1\nstderr：\ndisk full\nstdout：\nhalf done", body["content"])
        self.assertIn("時間：2026-10-30 15:05:00", body["content"])

    async def test_a_hung_script_is_stopped_and_reported(self):
        code, _ = await self.job("import time\ntime.sleep(30)", timeout_s=0.5)
        self.assertEqual(code, 1)
        self.assertIn("超過 0.5 秒還沒跑完", self.posts[0][1]["content"])

    async def test_nowhere_to_send_leaves_it_in_the_journal(self):
        code, printed = await self.job("print('ok')", deliver=None)
        self.assertEqual((code, printed, self.posts), (0, "ok\n", []))
        code, printed = await self.job("print('ok')", token="")
        self.assertEqual(code, 2)
        self.assertIn("沒有 DISCORD_TOKEN", printed)

    def test_targets_env_and_folder(self):
        self.assertEqual((parse_target("discord:155"), parse_target("none"), parse_target("")), ("155", None, None))
        with self.assertRaises(ValueError):
            parse_target("telegram:1")
        env = child_env({"PATH": "/usr/bin", "DISCORD_TOKEN": "x", "SNOWCAST_KEY": "y", "VIRTUAL_ENV": "/opt/niya/.venv"}, ["DISCORD_TOKEN"])
        self.assertEqual(env["SNOWCAST_KEY"], "y")
        self.assertNotIn("DISCORD_TOKEN", env)
        self.assertNotIn("NIYA_AGENT", env)
        script = self.dir / "check.py"
        script.write_text("")
        self.assertEqual(default_cwd(["/usr/bin/python3", str(script)]), self.dir)
        self.assertIsNone(default_cwd(["echo", "hi"]))


if __name__ == "__main__":
    unittest.main()
