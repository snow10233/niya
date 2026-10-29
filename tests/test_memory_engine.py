import unittest

from helpers import TZ, FakeLLM, HashEmbedder, reply, ts

from niya.agent.context import Conversation
from niya.agent.loop import Agent
from niya.clock import FakeClock
from niya.db import Database
from niya.inbox import InboundItem
from niya.memory.curve import DAY
from niya.memory.engine import MemoryEngine, core_ids
from niya.runtime import recall_hook
from niya.tools.registry import ToolRegistry


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.clock = FakeClock(ts("2026-10-28 10:31"))
        self.embedder = HashEmbedder()
        self.engine = MemoryEngine(self.db, self.clock, TZ, embedder=self.embedder)

    def make_active(self, memory_id, stability):
        self.db.execute("UPDATE memories SET state = 'active', stability = ? WHERE id = ?", (stability, memory_id))

    async def test_remember_is_labile_embedded_and_deduplicated(self):
        memory, new = await self.engine.remember("主人喝咖啡不加糖", kind="preference", importance=4, trust="owner", session_id="s1")
        self.assertTrue(new)
        self.assertEqual(memory.state, "labile")
        self.assertAlmostEqual(memory.stability, 1.96 * 0.05, places=2)
        self.assertEqual(self.db.one("SELECT model, dim FROM memory_vectors")[:], ("hash@256", 256))
        again, new = await self.engine.remember("主人喝咖啡不加糖", kind="preference")
        self.assertEqual((again.id, new), (memory.id, False))
        with self.assertRaises(ValueError):
            await self.engine.remember("x", kind="gossip")
        capped, _ = await self.engine.remember("網頁說這款咖啡機很好用", trust="external", importance=9)
        self.assertEqual(capped.importance, 6)

    async def test_recall_filters_by_cue_strength_session_and_core(self):
        coffee, _ = await self.engine.remember("主人喝咖啡不加糖，只加燕麥奶", kind="preference", trust="owner", session_id="old")
        nginx, _ = await self.engine.remember("nginx 設定檔在 /etc/nginx/sites-available/niya", session_id="old")
        here, _ = await self.engine.remember("主人今天想喝咖啡", session_id="now")
        self.make_active(coffee.id, 30)
        self.make_active(nginx.id, 30)
        self.clock.advance(DAY)
        result = await self.engine.recall("幫我泡咖啡，不加糖", session_id="now", turn_id="t1")
        self.assertEqual(result.ids, [coffee.id])  # the same-session memory is already in context
        self.assertIn(f"- m{coffee.id} [昨天上午 10 點・偏好] 主人喝咖啡不加糖", result.block)
        self.assertTrue(result.block.startswith("<recalled_memories>"))
        again = await self.engine.recall("咖啡不加糖嗎", session_id="now", turn_id="t2")
        self.assertNotIn(coffee.id, again.ids)  # already shown in this session
        excluded = await self.engine.recall("咖啡不加糖", session_id="other", turn_id="t3", exclude=[coffee.id])
        self.assertEqual(excluded.ids, [])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM retrieval_events WHERE channel = 'auto'")["n"], 1)
        self.assertIsNotNone(here)

    async def test_faded_memory_needs_a_strong_cue_and_shows_the_tip_of_the_tongue(self):
        memory, _ = await self.engine.remember("上次網站 502 是因為 gunicorn 沒有啟動", session_id="old")
        self.make_active(memory.id, 1)
        self.clock.advance(20 * DAY)  # R ≈ 0.31
        weak = await self.engine.recall("網站又掛了", session_id="now", turn_id="t1")
        self.assertEqual(weak.ids, [])
        strong = await self.engine.recall("上次網站 502 是因為 gunicorn 沒有啟動嗎", session_id="now", turn_id="t2")
        self.assertEqual(strong.ids, [memory.id])
        self.assertIn("印象模糊", strong.block)
        self.db.execute("UPDATE memories SET state = 'archived' WHERE id = ?", (memory.id,))
        archived = await self.engine.recall("gunicorn 沒有啟動", session_id="x", turn_id="t3")
        self.assertIn("還有 1 條可能相關、但想不太起來的記憶", archived.block)

    async def test_time_cue_pulls_in_what_happened_then(self):
        memory, _ = await self.engine.remember("幫主人部署了 nginx", kind="event", session_id="old")
        self.make_active(memory.id, 10)
        self.clock.set(ts("2026-10-29 09:00"))
        self.db.execute("UPDATE memories SET last_review_at = ? WHERE id = ?", (self.clock.now(), memory.id))
        self.assertEqual((await self.engine.recall("我們做了什麼", session_id="now", turn_id="t0")).ids, [])
        result = await self.engine.recall("昨天我們做了什麼", session_id="now", turn_id="t1")
        self.assertEqual(result.ids, [memory.id])

    async def test_sim_prev_tracks_topic_shifts(self):
        conv = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S")
        for i, text in enumerate(["nginx 設定檔在哪裡", "nginx 設定檔要怎麼改", "晚餐吃什麼好"]):
            conv.start_turn(f"t{i}", source="owner", ts=self.clock.now())
            await self.engine.recall(text, session_id=conv.session_id, turn_id=f"t{i}")
        sims = [r["sim_prev"] for r in self.db.query("SELECT sim_prev FROM turns ORDER BY started_at, id")]
        self.assertIsNone(sims[0])
        self.assertGreater(sims[1], sims[2])

    async def test_deep_recall_reaches_archived_memories_and_old_messages(self):
        memory, _ = await self.engine.remember("上次網站 502 是因為 gunicorn 沒有啟動", kind="event", session_id="old")
        self.db.execute("UPDATE memories SET state = 'archived' WHERE id = ?", (memory.id,))
        conv = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S")
        conv.add_user_items("t1", [InboundItem("owner", "gunicorn 又掛了嗎", self.clock.now())])
        text = await self.engine.deep_recall("gunicorn", session_id="now", turn_id="t9")
        self.assertIn(f"m{memory.id} [2026-10-28 (三) 10:31・事件・要很用力才想起來]", text)
        self.assertIn("msg#1 [2026-10-28 (三) 10:31] user: «gunicorn» 又掛了嗎", text)
        self.assertEqual(self.db.one("SELECT channel FROM retrieval_events")["channel"], "deep")

    async def test_timeline_and_related(self):
        event, _ = await self.engine.remember("幫主人部署 nginx", kind="event", session_id="old")
        fact, _ = await self.engine.remember("nginx 設定檔在 /etc/nginx/sites-available/niya", session_id="old")
        self.db.execute("INSERT INTO memory_links(src_id, dst_id, relation, created_at) VALUES (?, ?, 'derived_from', 0)", (fact.id, event.id))
        conv = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S")
        conv.start_turn("t1", source="owner", ts=self.clock.now())
        source = conv.add_user_items("t1", [InboundItem("owner", "幫我部署 nginx", self.clock.now())])
        self.db.execute("INSERT INTO memory_sources(memory_id, message_id) VALUES (?, 1)", (event.id,))
        timeline = self.engine.timeline(ts("2026-10-28 00:00"), ts("2026-10-29 00:00"), session_id="now", turn_id="t9")
        self.assertIn("幫主人部署 nginx", timeline)
        self.assertIn("10/28 1 回合", timeline)
        related = self.engine.related(f"m{fact.id}", session_id="now", turn_id="t9")
        self.assertIn(f"出自 m{event.id}", related)
        event_side = self.engine.related(f"m{event.id}", session_id="now", turn_id="t9")
        self.assertIn("當時的對話：", event_side)
        self.assertIn("user: 幫我部署 nginx", event_side)
        self.assertIsNone(source)
        with self.assertRaises(ValueError):
            self.engine.related("m999", session_id=None, turn_id=None)

    async def test_core_block_and_its_refs(self):
        pref, _ = await self.engine.remember("主人喝咖啡不加糖", kind="preference", trust="owner")
        await self.engine.remember("網頁說某咖啡機很好用", trust="external")
        plan, _ = await self.engine.remember("主人要看牙醫", kind="intention", trust="owner", occurred_start=ts("2026-11-02 14:00"), precision="minute")
        self.db.execute("INSERT INTO alarms(title, next_fire_at, late_grace_s, created_at) VALUES ('看牙醫', ?, 3600, 0)", (ts("2026-11-02 08:00"),))
        block = self.engine.core_block()
        self.assertTrue(block.startswith("# 記憶（10/28 10:31 整理）"))
        self.assertIn(f"## 關於主人\n- m{pref.id} 主人喝咖啡不加糖", block)
        self.assertNotIn("咖啡機", block)
        self.assertIn(f"- m{plan.id} 主人要看牙醫 [11/2（一）下午 2 點]", block)
        self.assertIn("- 鬧鐘 #1 2026-11-02 (一) 08:00 看牙醫", block)
        self.assertEqual(core_ids(f"S\n{block}"), {pref.id, plan.id})

    async def test_without_embeddings_recall_falls_back_to_full_text(self):
        memory, _ = await self.engine.remember("主人的貓叫麻糬", trust="owner", session_id="old")
        self.make_active(memory.id, 30)
        self.embedder.fail = True
        with self.assertLogs("niya.memory.engine", level="WARNING"):
            result = await self.engine.recall("麻糬好嗎", session_id="now", turn_id="t1")
        self.assertEqual(result.ids, [memory.id])  # a clear text match carries recall on its own
        with self.assertLogs("niya.memory.engine", level="WARNING"):
            self.assertIn("主人的貓叫麻糬", await self.engine.deep_recall("麻糬", session_id="now", turn_id="t2"))


class RecallInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recalled_block_sits_on_top_of_the_turn_and_stays_put(self):
        db = Database(":memory:")
        clock = FakeClock(ts("2026-10-28 10:31"))
        engine = MemoryEngine(db, clock, TZ, embedder=HashEmbedder())
        memory, _ = await engine.remember("主人喝咖啡不加糖", kind="preference", trust="owner", session_id="old")
        db.execute("UPDATE memories SET state = 'active', stability = 30 WHERE id = ?", (memory.id,))
        clock.advance(DAY)
        conv = Conversation.start_new(db, now=clock.now(), tz=TZ, system_prompt="S")
        llm = FakeLLM(reply("好，不加糖"), reply("嗯"))
        agent = Agent(llm=llm, model="m", tools=ToolRegistry(), conversation=conv, clock=clock, recall=recall_hook(engine))
        await agent.run_turn([InboundItem("owner", "幫我泡杯咖啡不加糖", clock.now())])
        first = llm.requests[0]["messages"][1]["content"]
        self.assertTrue(first.startswith("<recalled_memories>\n- m1 [昨天上午 10 點・偏好] 主人喝咖啡不加糖\n</recalled_memories>\n[2026-10-29 (四) 10:31] user: 幫我泡杯咖啡不加糖"))
        await agent.run_turn([InboundItem("owner", "謝謝", clock.now())])
        self.assertEqual(llm.requests[1]["messages"][1]["content"], first)


if __name__ == "__main__":
    unittest.main()
