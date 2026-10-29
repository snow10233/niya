import asyncio
import unittest

from niya.discord_bot.progress import ProgressBubble


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


class ProgressBubbleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.channel = FakeChannel()

        async def target():
            return self.channel

        self.bubble = ProgressBubble(target)

    async def test_a_burst_of_calls_becomes_one_message_with_few_edits(self):
        for i in range(10):
            self.bubble.add(f"💻 terminal: echo {i}")
        await self.bubble.flush()
        [message] = self.channel.sent
        self.assertIn("echo 0", message.content)
        self.assertIn("echo 9", message.content)
        self.assertLessEqual(message.edits, 1)

    async def test_each_turn_gets_its_own_message(self):
        self.bubble.add("💻 terminal: ls")
        await self.bubble.finish()
        self.bubble.add("📖 read_file: /etc/hosts")
        await self.bubble.flush()
        self.assertEqual(len(self.channel.sent), 2)
        self.assertNotIn("ls", self.channel.sent[1].content)

    async def test_a_failed_send_is_logged_not_raised(self):
        async def broken():
            raise RuntimeError("discord down")

        bubble = ProgressBubble(broken)
        with self.assertLogs("niya.discord_bot.progress", level="WARNING"):
            bubble.add("💻 terminal: ls")
            await bubble.flush()


if __name__ == "__main__":
    unittest.main()
