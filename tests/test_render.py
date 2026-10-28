import unittest

from helpers import TZ, reply, ts

from niya.agent.context import Conversation
from niya.agent.render import format_ts, render_line, strip_echoed_prefix
from niya.db import Database
from niya.inbox import InboundItem
from niya.llm import ToolCall
from niya.tools.registry import ToolOutcome


class FormatTests(unittest.TestCase):
    def test_timestamp_uses_timezone_and_chinese_weekday(self):
        self.assertEqual(format_ts(ts("2026-10-28 21:30"), TZ), "2026-10-28 (三) 21:30")
        self.assertEqual(format_ts(ts("2026-11-01 08:05"), TZ), "2026-11-01 (日) 08:05")

    def test_speaker_labels(self):
        self.assertEqual(render_line(ts("2026-10-28 21:30"), "owner", "嗨", TZ), "[2026-10-28 (三) 21:30] user: 嗨")
        self.assertEqual(render_line(ts("2026-10-28 21:30"), "alarm", "⏰", TZ), "[2026-10-28 (三) 21:30] alarm: ⏰")

    def test_strip_echoed_prefix(self):
        self.assertEqual(strip_echoed_prefix("[2026-10-28 (三) 21:31] niya: 好的"), "好的")
        self.assertEqual(strip_echoed_prefix("  [2026-10-28 (三) 21:31] niya：好的"), "好的")
        self.assertEqual(strip_echoed_prefix("好的 [2026-10-28 (三) 21:31] niya: x"), "好的 [2026-10-28 (三) 21:31] niya: x")
        self.assertEqual(strip_echoed_prefix("[連結] niya: 不是時間"), "[連結] niya: 不是時間")


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.conv = Conversation.start_new(self.db, now=ts("2026-10-29 06:12"), tz=TZ, system_prompt="SYS")

    def test_queued_items_are_one_message_sorted_by_time(self):
        self.conv.start_turn("t1", source="owner", ts=ts("2026-10-29 06:12"))
        self.conv.add_user_items(
            "t1",
            [
                InboundItem("system", "夜間睡眠醒來", ts("2026-10-29 06:12")),
                InboundItem("owner", "在嗎？", ts("2026-10-29 03:40")),
            ],
        )
        messages = self.conv.api_messages()
        self.assertEqual(messages[0], {"role": "system", "content": "SYS"})
        self.assertEqual(
            messages[1]["content"],
            "[2026-10-29 (四) 03:40] user: 在嗎？\n[2026-10-29 (四) 06:12] system: 夜間睡眠醒來",
        )

    def test_assistant_prefix_and_tool_messages(self):
        self.conv.start_turn("t1", source="owner", ts=ts("2026-10-29 09:15"), recall_block="<recalled_memories>\n- m1\n</recalled_memories>")
        self.conv.add_user_items("t1", [InboundItem("owner", "網站 502 了", ts("2026-10-29 09:15"))])
        self.conv.add_assistant("t1", reply(None, ("c1", "terminal", '{"command": "ls"}')), ts=ts("2026-10-29 09:15"))
        call = ToolCall("c1", "terminal", '{"command": "ls"}')
        self.conv.add_tool_result("t1", ToolOutcome(call=call, content="short", full_content="full output"), ts=ts("2026-10-29 09:16"))
        self.conv.add_assistant("t1", reply("[2026-10-29 (四) 09:16] niya: nginx 正常"), ts=ts("2026-10-29 09:16"))

        _, user, call_msg, tool, answer = self.conv.api_messages()
        self.assertTrue(user["content"].startswith("<recalled_memories>"))
        self.assertTrue(user["content"].endswith("[2026-10-29 (四) 09:15] user: 網站 502 了"))
        self.assertIsNone(call_msg["content"])
        self.assertEqual(call_msg["tool_calls"][0]["function"]["name"], "terminal")
        self.assertEqual(tool, {"role": "tool", "tool_call_id": "c1", "content": "short"})
        self.assertEqual(answer["content"], "[2026-10-29 (四) 09:16] niya: nginx 正常")
        self.assertEqual(self.conv.messages[-1].content, "nginx 正常")

    def test_rendering_is_prefix_stable(self):
        self.conv.start_turn("t1", source="owner", ts=ts("2026-10-29 09:00"))
        self.conv.add_user_items("t1", [InboundItem("owner", "第一句", ts("2026-10-29 09:00"))])
        self.conv.add_assistant("t1", reply("好"), ts=ts("2026-10-29 09:01"))
        before = self.conv.api_messages()
        self.assertEqual(before, self.conv.api_messages())

        self.conv.start_turn("t2", source="owner", ts=ts("2026-10-29 10:00"))
        self.conv.add_user_items("t2", [InboundItem("owner", "第二句", ts("2026-10-29 10:00"))])
        after = self.conv.api_messages()
        self.assertEqual(after[: len(before)], before)

        reloaded = Conversation(self.db, self.conv.session_id, TZ)
        self.assertEqual(reloaded.api_messages(), after)


if __name__ == "__main__":
    unittest.main()
