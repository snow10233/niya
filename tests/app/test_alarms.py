import asyncio
import json
import unittest

from helpers import TZ, ts

from niya.app.alarms import MAX_ACTIVE, AlarmError, Alarms, parse_datetime
from niya.core.clock import FakeClock
from niya.core.db import Database
from niya.core.inbox import Inbox
from niya.llm.openrouter import ToolCall
from niya.tools.alarm_tool import alarm_tools
from niya.tools.registry import ToolContext, ToolRegistry


class AlarmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.clock = FakeClock(ts("2026-10-28 21:31"))
        self.alarms = Alarms(self.db, self.clock, TZ)
        self.inbox = Inbox(self.db, self.clock)

    def fire_at(self, when):
        self.clock.set(ts(when))
        return self.alarms.fire_due(self.inbox, self.clock.now())

    def test_one_shot_fires_once_with_its_note(self):
        alarm = self.alarms.set(title="看牙醫", at=ts("2026-11-04 08:00"), note="13:00 再提醒出門")
        self.assertEqual(self.fire_at("2026-11-04 07:59"), [])
        self.assertEqual(self.fire_at("2026-11-04 08:00"), [alarm.id])
        [item] = self.inbox.take()
        self.assertEqual((item.speaker, item.meta), ("alarm", {"alarm_id": alarm.id}))
        self.assertEqual(item.content, "⏰ 提醒主人：看牙醫（#1，10/28 21:31 設定）\n備註：13:00 再提醒出門")
        self.assertEqual(self.alarms.get(alarm.id).status, "fired")
        self.assertEqual(self.fire_at("2026-11-04 09:00"), [])

    def test_late_within_grace_and_missed_beyond_it(self):
        late = self.alarms.set(title="喝水", at=ts("2026-10-29 10:00"))
        missed = self.alarms.set(title="倒垃圾", at=ts("2026-10-29 01:00"))
        self.fire_at("2026-10-29 10:20")
        items = self.inbox.take()
        self.assertEqual([i.speaker for i in items], ["alarm"])
        self.assertIn("遲了 20 分鐘", items[0].content)
        [notice] = self.inbox.take()
        self.assertEqual(notice.speaker, "system")
        self.assertIn("倒垃圾", notice.content)
        self.assertEqual((self.alarms.get(late.id).status, self.alarms.get(missed.id).status), ("fired", "missed"))

    def test_repeating_alarm_follows_cron_in_local_time_and_stops_at_max(self):
        daily = self.alarms.set(title="檢查備份", cron="0 9 * * *", purpose="task", max_fires=2)
        self.assertEqual(daily.next_fire_at, ts("2026-10-29 09:00"))
        self.assertFalse(daily.wake_from_sleep)
        self.fire_at("2026-10-29 09:00")
        self.assertEqual(self.alarms.get(daily.id).next_fire_at, ts("2026-10-30 09:00"))
        self.assertIn("該做的事：檢查備份（#1，重複 0 9 * * *）", self.inbox.take()[0].content)
        self.fire_at("2026-10-30 09:00")
        self.assertEqual(self.alarms.get(daily.id).status, "fired")

    def test_validation(self):
        with self.assertRaises(AlarmError):
            self.alarms.set(title="過去", at=ts("2026-10-28 20:00"))
        with self.assertRaises(AlarmError):
            self.alarms.set(title="太密", cron="*/5 * * * *")
        with self.assertRaises(AlarmError):
            self.alarms.set(title="壞掉", cron="every day")
        with self.assertRaises(AlarmError):
            self.alarms.set(title="沒時間")
        with self.assertRaises(AlarmError):
            parse_datetime("明天早上", TZ)
        for i in range(MAX_ACTIVE):
            self.alarms.set(title=f"a{i}", at=ts("2026-12-01 09:00"))
        with self.assertRaises(AlarmError):
            self.alarms.set(title="太多", at=ts("2026-12-01 09:00"))

    def test_cancel_and_snooze(self):
        alarm = self.alarms.set(title="開會", at=ts("2026-10-29 14:00"))
        moved = self.alarms.snooze(alarm.id, ts("2026-10-29 14:30"))
        self.assertEqual(moved.next_fire_at, ts("2026-10-29 14:30"))
        self.alarms.cancel(alarm.id)
        self.assertEqual(self.fire_at("2026-10-29 15:00"), [])
        with self.assertRaises(AlarmError):
            self.alarms.cancel(alarm.id)

    async def test_tool(self):
        registry = ToolRegistry(alarm_tools(self.alarms))
        ctx = ToolContext(session_id="s", turn_id="t", now=self.clock.now)

        async def call(**args):
            [outcome] = await registry.execute([ToolCall(id="c", name="alarm", arguments=json.dumps(args))], ctx)
            return outcome

        made = await call(action="set", title="喝水", in_minutes=10)
        self.assertEqual(made.content, "已設定鬧鐘 #1：2026-10-28 (三) 21:41 提醒主人「喝水」。")
        weekly = await call(action="set", title="週報", repeat="0 9 * * 1", purpose="task")
        self.assertIn("重複鬧鐘 #2（0 9 * * 1）：下次 2026-11-02 (一) 09:00", weekly.content)
        listing = await call(action="list")
        self.assertIn("#1 2026-10-28 (三) 21:41 提醒主人：喝水", listing.content)
        self.assertTrue((await call(action="set", title="x", at="昨天")).is_error)
        self.assertIn("延到 2026-10-28 (三) 22:01", (await call(action="snooze", id=1, in_minutes=30)).content)
        self.assertIn("已取消", (await call(action="cancel", id=2)).content)

    async def test_run_rings_due_alarms_and_wakes_when_one_is_added(self):
        task = asyncio.create_task(self.alarms.run(self.inbox))
        self.addAsyncCleanup(lambda: asyncio.gather(task, return_exceptions=True))
        await asyncio.sleep(0)
        self.alarms.set(title="馬上", at=self.clock.now() + 60)
        self.clock.advance(61)
        await asyncio.wait_for(self.inbox.wait(), 2)
        task.cancel()
        self.assertEqual(self.inbox.take()[0].speaker, "alarm")


if __name__ == "__main__":
    unittest.main()
