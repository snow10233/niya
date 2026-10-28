import json
import os
import tempfile
import unittest
from pathlib import Path

from niya.llm import ToolCall
from niya.tools.files import file_tools
from niya.tools.protected_paths import ProtectedPaths
from niya.tools.registry import ToolContext, ToolRegistry


class FileToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.data = self.dir / "niya-data"
        self.data.mkdir()
        self.sshd_answers = []

        async def fake_sshd(path):
            return self.sshd_answers.pop(0) if self.sshd_answers else None

        tools = file_tools(lambda: str(self.dir), ProtectedPaths([self.data]), sshd_validator=fake_sshd)
        self.registry = ToolRegistry(tools)
        self.ctx = ToolContext(session_id="s", turn_id="t", now=lambda: 0.0)

    async def call(self, name, **args):
        return (await self.registry.execute([ToolCall("c", name, json.dumps(args))], self.ctx))[0]

    async def test_write_then_read_with_line_numbers_and_paging(self):
        out = await self.call("write_file", path="notes/a.txt", content="一\n二\n三\n")
        self.assertIn("已建立", out.content)
        self.assertEqual((await self.call("read_file", path="notes/a.txt")).content, "1|一\n2|二\n3|三")
        page = await self.call("read_file", path=str(self.dir / "notes/a.txt"), offset=2, limit=1)
        self.assertEqual(page.content, "2|二\n…（第 2–2 行，共 3 行；用 offset=3 繼續讀）")
        self.assertIn("只有 3 行", (await self.call("read_file", path="notes/a.txt", offset=9)).content)

    async def test_read_errors(self):
        (self.dir / "config.yaml").write_text("x")
        missing = await self.call("read_file", path="confg.yaml")
        self.assertTrue(missing.is_error)
        self.assertIn("config.yaml", missing.content)
        (self.dir / "bin").write_bytes(b"\x00\x01")
        self.assertIn("二進位", (await self.call("read_file", path="bin")).content)

    async def test_write_keeps_file_mode(self):
        target = self.dir / "script.sh"
        target.write_text("old")
        os.chmod(target, 0o750)
        await self.call("write_file", path="script.sh", content="new")
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(target.stat().st_mode & 0o777, 0o750)

    async def test_patch_fuzzy_and_diff(self):
        target = self.dir / "app.py"
        target.write_text("def main():\n    return 1\n")
        out = await self.call("patch", path="app.py", old_string="def main():\n  return 1", new_string="def main():\n    return 2")
        self.assertFalse(out.is_error, out.content)
        self.assertIn("+    return 2", out.content)
        self.assertEqual(target.read_text(), "def main():\n    return 2\n")
        miss = await self.call("patch", path="app.py", old_string="does not exist", new_string="x")
        self.assertTrue(miss.is_error)

    async def test_protected_data_dir(self):
        out = await self.call("write_file", path=str(self.data / "niya.db"), content="x")
        self.assertTrue(out.is_error)
        self.assertIn("niya 自己的資料", out.content)
        link = self.dir / "sneaky"
        link.symlink_to(self.data)
        self.assertTrue((await self.call("write_file", path="sneaky/x", content="x")).is_error)

    async def test_authorized_keys_only_grows(self):
        keys = self.dir / ".ssh" / "authorized_keys"
        keys.parent.mkdir()
        keys.write_text("ssh-ed25519 AAAA owner\n")
        added = await self.call("write_file", path=str(keys), content="ssh-ed25519 AAAA owner\nssh-ed25519 BBBB new\n")
        self.assertFalse(added.is_error, added.content)
        removed = await self.call("patch", path=str(keys), old_string="ssh-ed25519 AAAA owner\n", new_string="")
        self.assertTrue(removed.is_error)
        self.assertIn("既有的 key", removed.content)
        self.assertIn("AAAA owner", keys.read_text())

    async def test_bad_sshd_config_is_rolled_back(self):
        config = self.dir / "sshd_config"
        config.write_text("Port 22\n")
        self.sshd_answers = ["line 1: Bad configuration option: Prot"]
        out = await self.call("write_file", path=str(config), content="Prot 2222\n")
        self.assertTrue(out.is_error)
        self.assertIn("已還原", out.content)
        self.assertEqual(config.read_text(), "Port 22\n")
        self.assertEqual((self.dir / "sshd_config.bak").read_text(), "Port 22\n")
        ok = await self.call("write_file", path=str(config), content="Port 2222\n")
        self.assertFalse(ok.is_error)

    async def test_search_content_and_files(self):
        (self.dir / "src").mkdir()
        (self.dir / "src" / "a.py").write_text("import os\nprint('hi')\n")
        (self.dir / "src" / "b.txt").write_text("hi there\n")
        content = await self.call("search_files", pattern="hi", path="src", file_glob="*.py")
        self.assertIn("a.py:2:print('hi')", content.content)
        self.assertNotIn("b.txt", content.content)
        files = await self.call("search_files", pattern="*.txt", target="files", path="src")
        self.assertTrue(files.content.endswith("b.txt"))
        count = await self.call("search_files", pattern="hi", path="src", output_mode="count")
        self.assertEqual(len(count.content.splitlines()), 2)
        none = await self.call("search_files", pattern="zzz", path="src")
        self.assertEqual(none.content, "沒有找到符合的結果。")


if __name__ == "__main__":
    unittest.main()
