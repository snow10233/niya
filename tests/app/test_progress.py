import unittest
from pathlib import Path

from niya.app.niya import Interface, Niya
from niya.core.config import Settings
from niya.tools.preview import ToolView

SECRET = "sk-or-v1-0123456789abcdef0123"


async def quiet(text):
    pass


async def no(question):
    return False


class ToolProgressTests(unittest.TestCase):
    def test_secrets_are_redacted_before_the_preview_cuts_them(self):
        views = []
        ui = Interface(channel="test", say=quiet, notice=quiet, confirm=no, progress=views.append)
        niya = Niya(Settings(home=Path("/tmp/niya-test-unused"), secrets={"OPENROUTER_API_KEY": SECRET}), ui)
        niya._tool_started("terminal", {"command": f"curl -H 'Authorization: Bearer {SECRET}' https://x.example"})
        niya._tool_started("read_file", {"path": "/etc/hosts"}, sub=True)
        self.assertNotIn(SECRET[:12], views[0].detail)
        self.assertTrue(views[0].detail.startswith("curl -H 'Authorization: Bearer "))
        self.assertEqual(views[1], ToolView("↳ 📖 read_file", "hosts"))


if __name__ == "__main__":
    unittest.main()
