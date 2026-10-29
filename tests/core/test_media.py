import asyncio
import base64
import io
import tempfile
import unittest
from pathlib import Path

from helpers import TZ, FakeLLM, reply, ts
from PIL import Image

from niya.agent.context import Conversation
from niya.agent.loop import Agent
from niya.core.clock import FakeClock
from niya.core.db import Database
from niya.core.inbox import InboundItem
from niya.core.media import IMAGE_TOKENS, MAX_EDGE, MediaStore, data_url, encoded, sniff
from niya.discord_bot.attachments import save_attachments
from niya.llm.openrouter import ToolCall, apply_cache_control
from niya.tools.files import file_tools
from niya.tools.protected_paths import ProtectedPaths
from niya.tools.registry import ToolContext, ToolOutcome, ToolRegistry


def png(path: Path, size=(40, 30), color=(200, 30, 30)) -> Path:
    Image.new("RGB", size, color).save(path, "PNG")
    return path


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_sniff_by_content_not_name(self):
        self.assertEqual(sniff(png(self.dir / "a.txt").read_bytes()[:16]), "image/png")
        self.assertEqual(sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertIsNone(sniff(b"hello world"))

    def test_small_images_pass_through_big_ones_are_scaled(self):
        small = png(self.dir / "small.png")
        mime, data = encoded(str(small))
        self.assertEqual((mime, base64.b64decode(data)), ("image/png", small.read_bytes()))
        big = png(self.dir / "big.png", size=(4000, 3000))
        mime, data = encoded(str(big))
        with Image.open(io.BytesIO(base64.b64decode(data))) as image:
            self.assertEqual(max(image.size), MAX_EDGE)
        self.assertTrue(data_url(str(big)).startswith(f"data:{mime};base64,"))

    def test_missing_or_broken_files_are_none(self):
        self.assertIsNone(data_url(str(self.dir / "gone.png")))
        broken = self.dir / "broken.png"
        broken.write_bytes(b"\x89PNG\r\n\x1a\nnot really")
        self.assertIsNone(encoded(str(broken)))

    def test_store_names_files_by_time(self):
        store = MediaStore(self.dir / "media", TZ)
        path = store.save("inbox", b"x", ".bin", now=ts("2026-10-30 01:02"))
        self.assertEqual(path.parent, self.dir / "media" / "inbox")
        self.assertTrue(path.name.startswith("20261030-010200-"))
        self.assertEqual(path.read_bytes(), b"x")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = Database(":memory:")
        self.conv = Conversation.start_new(self.db, now=ts("2026-10-30 09:00"), tz=TZ, system_prompt="SYS")
        self.picture = png(self.dir / "cat.png")

    def test_owner_pictures_become_image_parts(self):
        self.conv.start_turn("t1", source="owner", ts=ts("2026-10-30 09:00"))
        items = [InboundItem("owner", "看這隻貓", ts("2026-10-30 09:00"), {"images": [str(self.picture), str(self.dir / "gone.png")]})]
        self.conv.add_user_items("t1", items)
        _, user = self.conv.api_messages()
        text, image = user["content"]
        self.assertEqual(text["type"], "text")
        self.assertTrue(text["text"].startswith("[2026-10-30 (五) 09:00] user: 看這隻貓"))
        self.assertIn(f"圖片已經不在了：{self.dir / 'gone.png'}", text["text"])
        self.assertTrue(image["image_url"]["url"].startswith("data:image/png;base64,"))
        reloaded = Conversation(self.db, self.conv.session_id, TZ)
        self.assertEqual(reloaded.api_messages(), self.conv.api_messages())

    def test_tool_pictures_ride_on_the_tool_message(self):
        self.conv.start_turn("t1", source="owner", ts=ts("2026-10-30 09:00"))
        self.conv.add_user_items("t1", [InboundItem("owner", "截圖給我看", ts("2026-10-30 09:00"))])
        self.conv.add_assistant("t1", reply(None, ("c1", "browser_exec", "{}")), ts=ts("2026-10-30 09:01"))
        outcome = ToolOutcome(call=ToolCall("c1", "browser_exec", "{}"), content="ok", full_content="ok", images=(str(self.picture),))
        self.conv.add_tool_result("t1", outcome, ts=ts("2026-10-30 09:01"))
        tool = self.conv.api_messages()[-1]
        self.assertEqual(tool["role"], "tool")
        self.assertEqual([p["type"] for p in tool["content"]], ["text", "image_url"])

    def test_cache_breakpoint_lands_on_the_last_part(self):
        messages = [{"role": "system", "content": "S"}, {"role": "user", "content": [{"type": "text", "text": "看"}, {"type": "image_url", "image_url": {"url": "data:x"}}]}]
        marked = apply_cache_control("anthropic/claude-opus-5.5", messages)
        self.assertNotIn("cache_control", marked[1]["content"][0])
        self.assertEqual(marked[1]["content"][1]["cache_control"], {"type": "ephemeral"})

    def test_pictures_count_toward_the_context_estimate(self):
        agent = Agent(llm=FakeLLM(), model="m", tools=ToolRegistry(), conversation=self.conv, clock=FakeClock(ts("2026-10-30 09:00")))
        self.conv.start_turn("t1", source="owner", ts=ts("2026-10-30 09:00"))
        self.conv.add_user_items("t1", [InboundItem("owner", "a", ts("2026-10-30 09:00"), {"images": [str(self.picture)] * 2})])
        self.assertGreaterEqual(agent.context_tokens(), 2 * IMAGE_TOKENS)


class ReadImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_file_attaches_pictures(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        picture = png(root / "shot.png")
        registry = ToolRegistry(file_tools(lambda: str(root), ProtectedPaths([])))
        ctx = ToolContext("s", "t", now=lambda: 0.0)
        [outcome] = await registry.execute([ToolCall("c1", "read_file", f'{{"path": "{picture}"}}')], ctx)
        self.assertFalse(outcome.is_error)
        self.assertIn("40×30", outcome.content)
        self.assertEqual(outcome.images, (str(picture),))


class FakeAttachment:
    def __init__(self, filename, data, size=None, fail=False):
        self.filename, self.data, self.size, self.fail = filename, data, len(data) if size is None else size, fail
        self.url = f"https://cdn.discordapp.com/{filename}"

    async def read(self):
        if self.fail:
            raise RuntimeError("404")
        return self.data


class AttachmentTests(unittest.TestCase):
    def test_pictures_files_and_failures(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        picture = png(root / "p.png").read_bytes()
        attachments = [
            FakeAttachment("貓.jpeg", picture),  # named jpeg, really a png
            FakeAttachment("notes.md", b"# hi"),
            FakeAttachment("huge.mp4", b"", size=100 * 1024 * 1024),
            FakeAttachment("broken.txt", b"", fail=True),
        ]
        lines, images = asyncio.run(save_attachments(attachments, MediaStore(root / "media", TZ), now=ts("2026-10-30 09:00")))
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].endswith(".png"))
        self.assertEqual(Path(images[0]).read_bytes(), picture)
        self.assertEqual(lines[0], f"[圖片] 貓.jpeg：{images[0]}")
        self.assertTrue(lines[1].startswith("[附件] notes.md：") and lines[1].endswith(".md"))
        self.assertIn("太大沒有下載", lines[2])
        self.assertIn("下載失敗", lines[3])


if __name__ == "__main__":
    unittest.main()
