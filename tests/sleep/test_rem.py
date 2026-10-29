import json
import unittest

from helpers import TZ, HashEmbedder, ts

from niya.core.clock import FakeClock
from niya.core.db import Database
from niya.memory.curve import initial_stability
from niya.memory.engine import MemoryEngine
from niya.sleep.rem import WATERMARK_KEY, rem
from niya.sleep.summaries import period_summaries


class Scripted:
    def __init__(self, **by_task):
        self.by_task = by_task
        self.prompts = []

    async def complete(self, *, system, prompt, task):
        self.prompts.append((task, prompt))
        value = self.by_task[task]
        return json.dumps(value.pop(0) if isinstance(value, list) else value, ensure_ascii=False)


class LongTermTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.clock = FakeClock(ts("2026-11-02 03:00"))  # Monday night sleep
        self.engine = MemoryEngine(self.db, self.clock, TZ, embedder=HashEmbedder())

    async def memory(self, content, *, kind="event", level=None, at="2026-10-28 10:00", state="active", stability=5.0):
        [vector] = await self.engine.embed([content])
        made = self.engine.create(kind=kind, level=level, content=content, importance=5, occurred_start=ts(at), occurred_end=ts(at), precision="day", origin="test", vector=vector)
        self.db.execute("UPDATE memories SET state = ?, stability = ? WHERE id = ?", (state, stability, made.id))
        return self.engine.get(made.id)

    async def test_using_an_event_strengthens_its_day_and_week(self):
        event = await self.memory("部署 nginx")
        day = await self.memory("10/28：部署 nginx", kind="summary", level="day")
        week = await self.memory("那週：架站", kind="summary", level="week", state="archived")
        self.engine.add_link(event.id, day.id, "part_of")
        self.engine.add_link(day.id, week.id, "part_of")
        self.clock.set(ts("2026-11-20 10:00"))
        self.assertTrue(self.engine.review(event, kind="use", ref_id="t1", at=self.clock.now()))
        reviews = {r["memory_id"]: r["kind"] for r in self.db.query("SELECT * FROM memory_reviews")}
        self.assertEqual(reviews, {event.id: "use", day.id: "parent", week.id: "parent"})
        self.assertGreater(self.engine.get(day.id).stability, 5.0)
        self.assertLess(self.engine.get(day.id).stability / 5.0, self.engine.get(event.id).stability / 5.0)
        self.assertEqual(self.engine.get(week.id).state, "archived")  # a nudge from below does not revive it
        self.assertTrue(self.engine.review(self.engine.get(week.id), kind="use", ref_id="t2", at=self.clock.now()))
        self.assertEqual(self.engine.get(week.id).state, "active")

    async def test_week_and_month_summaries_catch_up_once(self):
        days = [await self.memory(f"10/{d}：做了第 {d} 件事", kind="summary", level="day", at=f"2026-10-{d} 12:00") for d in (26, 28, 30)]
        old_day = await self.memory("10/05：很久以前", kind="summary", level="day", at="2026-10-05 12:00")
        completer = Scripted(week_summary={"summary": "這週架站、修 bug。", "importance": 6}, month_summary={"summary": "十月主要在架站。", "importance": 7})
        made = await period_summaries(self.engine, completer)
        self.assertEqual([(m.level, m.content) for m in made], [("week", "10/26–11/01 這週：這週架站、修 bug。"), ("month", "2026 年 10 月：十月主要在架站。")])
        week, month = made
        self.assertEqual((week.occurred_start, week.importance), (ts("2026-10-26 00:00"), 6))
        self.assertAlmostEqual(week.stability, initial_stability(6) * 3)
        week_children = {r["src_id"] for r in self.db.query("SELECT src_id FROM memory_links WHERE dst_id = ? AND relation = 'part_of'", (week.id,))}
        self.assertEqual(week_children, {d.id for d in days})
        self.assertEqual([r["src_id"] for r in self.db.query("SELECT src_id FROM memory_links WHERE dst_id = ?", (month.id,))], [week.id])
        self.assertNotIn(old_day.content, "".join(p for _, p in completer.prompts))  # only the last complete week and month
        self.assertEqual(await period_summaries(self.engine, completer), [])

    async def test_rem_links_similar_memories_and_draws_a_weak_insight(self):
        old = await self.memory("網站 502，因為 gunicorn 沒有啟動", at="2026-09-01 10:00")
        self.db.set_meta(WATERMARK_KEY, str(old.id))
        self.clock.set(ts("2026-11-02 03:00"))
        fresh = [await self.memory(text, at="2026-11-01 10:00") for text in ("網站又 502，gunicorn 沒有啟動", "網站 502 了，重啟 gunicorn 就好")]
        for m in fresh:
            self.db.execute("UPDATE memories SET created_at = ? WHERE id = ?", (ts("2026-11-01 12:00"), m.id))
        completer = Scripted(rem={"insight": "網站 502 通常是 gunicorn 沒有啟動，先檢查它。", "importance": 6})
        stats = await rem(self.engine, completer)
        self.assertEqual(stats.insights, ["網站 502 通常是 gunicorn 沒有啟動，先檢查它。"])
        insight = self.db.one("SELECT * FROM memories WHERE kind = 'insight'")
        self.assertAlmostEqual(insight["stability"], initial_stability(6) * 0.5)
        self.assertEqual({r["dst_id"] for r in self.db.query("SELECT dst_id FROM memory_links WHERE relation = 'abstracts'")}, {old.id, *(m.id for m in fresh)})
        self.assertGreater(stats.linked, 0)
        self.assertEqual(int(self.db.get_meta(WATERMARK_KEY)), fresh[-1].id)  # where REM picks up next night
        again = await rem(self.engine, Scripted(rem={"insight": None}))
        self.assertEqual(again.insights, [])

    async def test_rem_without_a_pattern_adds_nothing(self):
        for text in ("網站 502，gunicorn 沒有啟動", "網站又 502，gunicorn 沒有啟動", "網站 502，重啟 gunicorn"):
            m = await self.memory(text, at="2026-11-01 10:00")
            self.db.execute("UPDATE memories SET created_at = ? WHERE id = ?", (ts("2026-11-01 12:00"), m.id))
        stats = await rem(self.engine, Scripted(rem={"insight": None, "importance": 1}))
        self.assertEqual((stats.clusters, stats.insights), (1, []))
        self.assertIsNone(self.db.one("SELECT 1 FROM memories WHERE kind = 'insight'"))


if __name__ == "__main__":
    unittest.main()
