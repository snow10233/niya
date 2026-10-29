import unittest
from pathlib import Path

from niya.app.niya import Interface, Niya
from niya.core.config import Settings

SECRET = "sk-or-v1-0123456789abcdef0123"


async def quiet(text):
    pass


async def no(question):
    return False


class ToolProgressTests(unittest.TestCase):
    def test_lines_are_redacted_and_subagent_calls_marked(self):
        lines = []
        ui = Interface(channel="test", say=quiet, notice=quiet, confirm=no, progress=lines.append)
        niya = Niya(Settings(home=Path("/tmp/niya-test-unused"), secrets={"OPENROUTER_API_KEY": SECRET}), ui)
        niya._tool_started("terminal", {"command": f"echo {SECRET}"})
        niya._tool_started("read_file", {"path": "/etc/hosts"}, sub=True)
        self.assertNotIn(SECRET, lines[0])
        self.assertTrue(lines[0].startswith("💻 terminal: echo "))
        self.assertEqual(lines[1], "↳ 📖 read_file: /etc/hosts")


if __name__ == "__main__":
    unittest.main()
