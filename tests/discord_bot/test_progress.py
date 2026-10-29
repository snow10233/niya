import asyncio
import unittest

from niya.discord_bot.progress import ProgressBubble
from niya.tools.preview import ToolView


class FakeMessage:
    def __init__(self, content):
        self.content = content
        self.edits = 0

    async def edit(self, *, content):
        await asyncio.sleep(0)
        self.content = content
        self.edits += 1


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content):
        await asyncio.sleep(0.01)
        message = FakeMessage(content)
        self.sent.append(message)
        return message


def command(text):
    return ToolView("💻 terminal", text)


class ProgressBubbleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.channel = FakeChannel()

        async def target():
            return self.channel

        self.target = target
        self.bubble = ProgressBubble(target)

    async def test_a_burst_of_calls_becomes_one_message_with_few_edits(self):
        for i in range(10):
            self.bubble.add(command(f"echo {i}"))
        await self.bubble.flush()
        [message] = self.channel.sent
        self.assertEqual(message.content.count("💻 terminal"), 1)
        self.assertEqual(message.content.count("```\necho "), 10)
        self.assertLessEqual(message.edits, 1)

    async def test_the_same_call_again_is_counted(self):
        self.bubble.add(command("git status"))
        self.bubble.add(command("git status"))
        await self.bubble.flush()
        self.assertTrue(self.channel.sent[0].content.endswith("``` (×2)"))

    async def test_a_full_message_continues_in_a_new_one(self):
        bubble = ProgressBubble(self.target, limit=60)
        for i in range(4):
            bubble.add(command(f"echo {i} " + "x" * 10))
        await bubble.flush()
        self.assertGreater(len(self.channel.sent), 1)
        self.assertTrue(all(len(m.content) <= 60 for m in self.channel.sent))
        self.assertTrue(all(m.content.startswith("💻 terminal\n") for m in self.channel.sent))

    async def test_after_finish_the_next_call_starts_a_new_message(self):
        self.bubble.add(command("ls"))
        await self.bubble.finish()
        self.bubble.add(ToolView("📖 read_file", "hosts"))
        await self.bubble.flush()
        self.assertEqual(len(self.channel.sent), 2)
        self.assertEqual(self.channel.sent[1].content, "📖 read_file\n```\nhosts\n```")

    async def test_a_failed_send_is_logged_not_raised(self):
        async def broken():
            raise RuntimeError("discord down")

        bubble = ProgressBubble(broken)
        with self.assertLogs("niya.discord_bot.progress", level="WARNING"):
            bubble.add(command("ls"))
            await bubble.flush()


if __name__ == "__main__":
    unittest.main()
