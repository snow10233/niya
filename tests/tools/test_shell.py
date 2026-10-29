import os
import tempfile
import time
import unittest
from pathlib import Path

from niya.tools.shell import BoundedOutput, ShellSession, clean_env


class ShellTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = dict(os.environ, NIYA_TEST_SECRET="s3cret", VIRTUAL_ENV="/venv")
        self.shell = ShellSession(self.dir / "state", cwd=str(self.dir), env=env, secret_names=["NIYA_TEST_SECRET"])
        self.addCleanup(self.shell.close)

    async def test_output_and_exit_code(self):
        result = await self.shell.run("echo hello; echo oops >&2; exit 3", timeout=10)
        self.assertEqual(result.output.strip().splitlines(), ["hello", "oops"])
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.timed_out)

    async def test_cwd_and_exports_persist(self):
        (self.dir / "sub").mkdir()
        await self.shell.run("cd sub && export GREETING=hi && myfn() { echo fn-ok; }", timeout=10)
        self.assertEqual(self.shell.cwd, str((self.dir / "sub").resolve()))
        result = await self.shell.run("pwd; echo $GREETING; myfn", timeout=10)
        self.assertEqual(result.output.strip().splitlines(), [str((self.dir / "sub").resolve()), "hi", "fn-ok"])

    async def test_secrets_and_venv_are_not_visible(self):
        result = await self.shell.run("echo [$NIYA_TEST_SECRET][$VIRTUAL_ENV][$NIYA_AGENT]", timeout=10)
        self.assertEqual(result.output.strip(), "[][][1]")
        await self.shell.run("export NIYA_TEST_SECRET=leaked", timeout=10)
        self.assertNotIn("leaked", self.shell.snapshot.read_text())

    async def test_timeout_kills_the_command(self):
        started = time.monotonic()
        result = await self.shell.run("echo start; sleep 30", timeout=0.5)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        self.assertIn("start", result.output)
        self.assertLess(time.monotonic() - started, 5)

    async def test_background_child_does_not_hang(self):
        started = time.monotonic()
        result = await self.shell.run("sleep 30 & echo launched", timeout=10)
        self.assertIn("launched", result.output)
        self.assertLess(time.monotonic() - started, 3)

    async def test_stdin_and_marker_hidden(self):
        result = await self.shell.run("printf 'no newline'", timeout=10, stdin=None)
        self.assertEqual(result.output, "no newline")
        piped = await self.shell.run("cat", timeout=10, stdin="from stdin")
        self.assertEqual(piped.output, "from stdin")

    async def test_explicit_cwd_does_not_move_session(self):
        before = self.shell.cwd
        result = await self.shell.run("pwd", timeout=10, cwd="/")
        self.assertEqual(result.output.strip(), "/")
        self.assertEqual(self.shell.cwd, before)


class HelperTests(unittest.TestCase):
    def test_bounded_output_keeps_head_and_tail(self):
        sink = BoundedOutput(limit=10)
        for piece in ["abcde", "fghij", "klmno"]:
            sink.add(piece)
        self.assertEqual(sink.text(), "abcde\n…（輸出過長，中間省略 5 字元）…\nklmno")

    def test_clean_env(self):
        env = clean_env({"PATH": "/bin", "OPENROUTER_API_KEY": "k", "PYTHONPATH": "x"}, ["OPENROUTER_API_KEY"])
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["PAGER"], "cat")



class CleanEnvTests(unittest.TestCase):
    def test_niyas_own_virtualenv_is_left_out(self):
        from niya.tools.shell import clean_env

        env = clean_env({"VIRTUAL_ENV": "/opt/niya/.venv", "PATH": "/opt/niya/.venv/bin:/usr/local/bin:/usr/bin", "OPENROUTER_API_KEY": "k"}, ["OPENROUTER_API_KEY"])
        self.assertEqual(env["PATH"], "/usr/local/bin:/usr/bin")
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertNotIn("OPENROUTER_API_KEY", env)


if __name__ == "__main__":
    unittest.main()
