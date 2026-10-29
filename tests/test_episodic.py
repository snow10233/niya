import json
import unittest

from helpers import TZ, ts

from niya.agent.context import Conversation
from niya.db import Database
from niya.inbox import InboundItem, turn_source
from niya.memory.episodic import close_idle_segments
from niya.memory.search import query_terms, search_messages
from niya.tools.memory_tools import memory_tools, parse_bound
from niya.tools.registry import ToolCall, ToolContext, ToolError, ToolRegistry


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.conv = Conversation.start_new(self.db, now=ts("2026-10-29 09:00"), tz=TZ, system_prompt="S")
        self.n = 0

    def turn(self, at, source="owner", minutes=2):
        self.n += 1
        turn_id = f"t{self.n}"
        self.conv.start_turn(turn_id, source=source, ts=ts(at))
        self.conv.finish_turn(turn_id, ts=ts(at) + minutes * 60)
        return self.db.one("SELECT segment_id FROM turns WHERE id = ?", (turn_id,))["segment_id"]

    def segments(self):
        return [(r["kind"], r["status"], r["closed_reason"]) for r in self.db.query("SELECT * FROM segments ORDER BY id")]

    def test_idle_gap_alarm_and_session_end(self):
        first = self.turn("2026-10-29 09:00")
        self.assertEqual(self.turn("2026-10-29 09:20"), first)  # 18 minutes after the last turn ended
        alarm = self.turn("2026-10-29 09:30", source="alarm")
        self.assertNotEqual(alarm, first)
        self.assertEqual(self.turn("2026-10-29 09:40"), first)  # the alarm does not break up the chat
        later = self.turn("2026-10-29 10:20")  # 38 minutes of quiet
        self.assertNotEqual(later, first)
        self.conv.end(now=ts("2026-10-29 11:00"), reason="manual")
        self.assertEqual(self.segments(), [("chat", "closed", "idle"), ("alarm", "closed", "alarm"), ("chat", "closed", "session_end")])
        ended = self.db.one("SELECT ended_at FROM segments WHERE id = ?", (first,))["ended_at"]
        self.assertEqual(ended, ts("2026-10-29 09:42"))

    def test_close_idle_segments(self):
        self.turn("2026-10-29 09:00")
        self.assertEqual(close_idle_segments(self.db, now=ts("2026-10-29 09:20")), 0)
        self.assertEqual(close_idle_segments(self.db, now=ts("2026-10-29 09:40")), 1)
        self.assertEqual(self.segments(), [("chat", "closed", "idle")])

    def test_turn_source(self):
        now = ts("2026-10-29 09:00")
        self.assertEqual(turn_source([InboundItem("system", "醒來", now), InboundItem("owner", "嗨", now + 1)]), "owner")
        self.assertEqual(turn_source([InboundItem("process", "done", now + 5), InboundItem("system", "x", now)]), "system")
        self.assertEqual(turn_source([InboundItem("alarm", "⏰", now)]), "alarm")


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        old = Conversation.start_new(self.db, now=ts("2026-08-03 14:00"), tz=TZ, system_prompt="S")
        old.add_user_items("t1", [InboundItem("owner", "網站又 502 了，幫我看一下 nginx", ts("2026-08-03 14:12"))])
        old.add_tool_result("t1", _outcome("c1", "terminal", "nginx: 502 upstream gunicorn"), ts=ts("2026-08-03 14:13"))
        self.answer = old.add_assistant("t1", _reply("上次 502 是 gunicorn 沒啟動，我重啟了"), ts=ts("2026-08-03 14:15"))
        old.add_user_items("t2", [InboundItem("owner", "我喝咖啡不加糖", ts("2026-09-10 08:00"))])
        old.add_user_items("t3", [InboundItem("owner", "記得設定檔在 /etc/nginx/sites-available/niya", ts("2026-10-28 10:31"))])
        old.end(now=ts("2026-10-28 12:00"), reason="manual")
        self.now = Conversation.start_new(self.db, now=ts("2026-10-29 09:00"), tz=TZ, system_prompt="S")
        self.now.add_user_items("t9", [InboundItem("owner", "nginx 現在怎樣", ts("2026-10-29 09:00"))])
        self.registry = ToolRegistry(memory_tools(self.db, TZ))
        self.ctx = ToolContext(session_id=self.now.session_id, turn_id="t10", now=lambda: ts("2026-10-29 09:05"))

    def test_query_terms(self):
        self.assertEqual(query_terms("nginx 設定檔在哪"), (["nginx", "設定檔", "定檔在", "檔在哪"], ["設定", "定檔", "檔在", "在哪"]))
        self.assertEqual(query_terms("咖啡 ok a"), ([], ["咖啡", "ok"]))

    def test_trigram_search_skips_tools_and_current_session(self):
        hits = search_messages(self.db, "nginx", exclude_session=self.now.session_id)
        self.assertEqual({h.role for h in hits}, {"user"})
        self.assertEqual(len(hits), 2)
        self.assertTrue(any("«" in h.snippet for h in hits))
        self.assertEqual(len(search_messages(self.db, "nginx")), 3)

    def test_short_terms_and_sentences_fall_back_to_like(self):
        hits = search_messages(self.db, "咖啡")
        self.assertEqual([h.snippet for h in hits], ["我喝«咖啡»不加糖"])
        self.assertEqual(search_messages(self.db, "上次的咖啡")[0].snippet, "我喝«咖啡»不加糖")
        self.assertEqual(search_messages(self.db, "100%"), [])

    def test_time_bounds(self):
        since = parse_bound("2026-09-01", TZ)
        self.assertEqual([h.id for h in search_messages(self.db, "nginx", since=since, exclude_session=self.now.session_id)], [5])
        self.assertEqual(parse_bound("2026-10-28", TZ, end=True), ts("2026-10-29 00:00"))
        self.assertEqual(parse_bound("2026-10-28 18:00", TZ, end=True), ts("2026-10-28 18:01"))
        with self.assertRaises(ToolError):
            parse_bound("昨天", TZ)

    async def call(self, name, **args):
        [outcome] = await self.registry.execute([ToolCall(id="c", name=name, arguments=json.dumps(args))], self.ctx)
        return outcome

    async def test_deep_recall_and_related_tools(self):
        found = await self.call("deep_recall", query="gunicorn")
        self.assertIn(f"msg#{self.answer.id} [2026-08-03 (一) 14:15] niya: 上次 502 是 «gunicorn» 沒啟動", found.content)
        context = await self.call("recall_related", ref=f"msg#{self.answer.id}")
        lines = context.content.splitlines()
        self.assertIn("[2026-08-03 (一) 14:12] user: 網站又 502 了", lines[1])
        self.assertIn("（terminal 的輸出，", lines[2])
        self.assertTrue(lines[3].startswith(f"→ msg#{self.answer.id}"))
        self.assertIn("找不到", (await self.call("deep_recall", query="火星")).content)
        self.assertTrue((await self.call("recall_related", ref="m12")).is_error)


def _outcome(call_id, name, text):
    from niya.tools.registry import ToolOutcome

    return ToolOutcome(call=ToolCall(id=call_id, name=name, arguments="{}"), content=text, full_content=text)


def _reply(text):
    from helpers import reply

    return reply(text)


if __name__ == "__main__":
    unittest.main()
