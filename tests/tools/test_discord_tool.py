import json
import tempfile
import unittest
from pathlib import Path

import httpx
from helpers import TZ, ts

from niya.agent.context import Conversation
from niya.core.clock import FakeClock
from niya.core.db import Database
from niya.llm.openrouter import ToolCall
from niya.tools.discord_tool import DiscordRest, discord_tools
from niya.tools.egress import EgressGate
from niya.tools.registry import ToolContext, ToolRegistry

GUILD, OWNER, BOT = "100", "7", "999"


class FakeDiscord:
    def __init__(self):
        self.requests = []
        self.channels = {
            "1": {"id": "1", "type": 0, "name": "general", "guild_id": GUILD, "position": 0},
            "2": {"id": "2", "type": 0, "name": "news", "guild_id": GUILD, "position": 1, "parent_id": "9"},
            "9": {"id": "9", "type": 4, "name": "Feeds", "guild_id": GUILD, "position": 0},
            "50": {"id": "50", "type": 0, "name": "other-server", "guild_id": "555"},
            "77": {"id": "77", "type": 1, "recipients": [{"id": OWNER}]},
        }
        self.messages = {
            "11": {"id": "11", "timestamp": "2026-10-30T01:00:00+00:00", "author": {"id": BOT, "username": "niya", "bot": True}, "content": "我發的"},
            "12": {"id": "12", "timestamp": "2026-10-30T01:05:00+00:00", "author": {"id": OWNER, "global_name": "snow"}, "content": "主人說的",
                   "embeds": [{"title": "新聞標題", "url": "https://news.example/a"}]},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path.removeprefix("/api/v10"), request.method
        if path == "/users/@me":
            return httpx.Response(200, json={"id": BOT})
        if path == "/users/@me/channels":
            return httpx.Response(200, json=self.channels["77"])
        if path == f"/guilds/{GUILD}/channels":
            return httpx.Response(200, json=[c for c in self.channels.values() if c.get("guild_id") == GUILD])
        if path == f"/guilds/{GUILD}/threads/active":
            return httpx.Response(200, json={"threads": [{"id": "3", "type": 11, "name": "閒聊串", "parent_id": "1", "guild_id": GUILD}]})
        parts = path.strip("/").split("/")
        if parts[0] == "channels" and len(parts) == 2:
            if method == "PATCH":
                return httpx.Response(200, json={**self.channels[parts[1]], **json.loads(request.content)})
            channel = self.channels.get(parts[1])
            return httpx.Response(200, json=channel) if channel else httpx.Response(404, json={"message": "Unknown Channel"})
        if parts[0] == "channels" and parts[2] == "messages":
            if len(parts) == 4:
                if method == "DELETE":
                    return httpx.Response(204)
                return httpx.Response(200, json=self.messages[parts[3]])
            if method == "GET":
                return httpx.Response(200, json=[self.messages["12"], self.messages["11"]])
            return httpx.Response(200, json={"id": f"m{len(self.requests)}"})
        if parts[0] == "channels" and parts[2] == "threads":
            return httpx.Response(200, json={"id": "4", "name": json.loads(request.content)["name"]})
        return httpx.Response(404, json={"message": "nope"})


class DiscordToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fake = FakeDiscord()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.fake))
        self.addAsyncCleanup(self.http.aclose)
        self.clock = FakeClock(ts("2026-10-30 09:00"))
        self.db = Database(":memory:")
        self.session = Conversation.start_new(self.db, now=self.clock.now(), tz=TZ, system_prompt="S").session_id
        self.notices = []

        async def notify(text):
            self.notices.append(text)

        self.gate = EgressGate(self.db, now=self.clock.now, notify=notify)
        self.media = self.dir / "media"
        self.media.mkdir()
        self.current = None
        rest = DiscordRest("token", self.http, guild_id=int(GUILD), owner_id=int(OWNER))
        tools = discord_tools(
            rest, tz=TZ, current_channel=lambda: self.current, gate=self.gate, redact=lambda t: t.replace("sk-secret", "***"),
            free_dirs=[self.media], protected=lambda p: "niya 的資料" if p.name == "niya.db" else None,
        )
        self.registry = ToolRegistry(tools)

    async def call(self, **args):
        ctx = ToolContext(self.session, "t1", now=self.clock.now)
        [outcome] = await self.registry.execute([ToolCall("c1", "discord", json.dumps(args))], ctx)
        return outcome

    def posts(self):
        return [r for r in self.fake.requests if r.method == "POST" and r.url.path.endswith("/messages")]

    async def test_list_channels_nests_categories_and_threads(self):
        out = (await self.call(action="list_channels")).content
        self.assertIn("general（文字，1）\n  閒聊串（討論串，3）", out)
        self.assertIn("Feeds（分類，9）\n  news（文字，2）", out)

    async def test_read_is_external_and_in_order(self):
        outcome = await self.call(action="read", channel_id="1", limit=2)
        self.assertEqual(outcome.external_source, "discord")
        self.assertLess(outcome.content.index("我發的"), outcome.content.index("主人說的"))
        self.assertIn("[2026-10-30 (五) 09:05] snow: 主人說的  (msg 12)", outcome.content)
        self.assertIn("[嵌入] 新聞標題 https://news.example/a", outcome.content)

    async def test_send_defaults_to_where_the_owner_is_then_dm(self):
        self.current = "1"
        self.assertIn("#general", (await self.call(action="send", text="嗨 sk-secret")).content)
        self.assertEqual(json.loads(self.posts()[-1].content)["content"], "嗨 ***")
        self.current = None
        self.assertIn("主人的私訊", (await self.call(action="send", text="私訊")).content)
        self.assertTrue(self.posts()[-1].url.path.endswith("/channels/77/messages"))

    async def test_send_refuses_other_servers_and_private_files(self):
        self.assertIn("不在主人的伺服器", (await self.call(action="send", channel_id="50", text="x")).content)
        db = self.dir / "niya.db"
        db.write_bytes(b"x")
        self.assertIn("niya 的資料", (await self.call(action="send", channel_id="1", files=[str(db)])).content)
        self.assertEqual(self.posts(), [])

    async def test_files_attach_and_outside_media_waits_for_the_owner_when_tainted(self):
        picture = self.media / "cat.png"
        picture.write_bytes(b"png")
        report = self.dir / "report.txt"
        report.write_text("hi")
        self.gate.mark_tainted(self.session, "t1", "web_extract:evil.example")
        out = (await self.call(action="send", channel_id="1", text="看", files=[str(picture)])).content
        self.assertIn("附了 1 個檔案", out)
        self.assertIn(b'filename="cat.png"', self.posts()[-1].content)
        held = (await self.call(action="send", channel_id="1", files=[str(report)])).content
        self.assertIn("已暫停", held)
        self.assertIn("discord.com", self.notices[0])
        self.assertEqual(len(self.posts()), 1)
        self.gate.resolve_owner_reply(self.session, "好")
        self.assertIn("已傳到", (await self.call(action="send", channel_id="1", files=[str(report)])).content)

    async def test_delete_only_own_messages(self):
        self.assertIn("只能刪你自己發的", (await self.call(action="delete", channel_id="1", message_id="12")).content)
        self.assertIn("刪掉了", (await self.call(action="delete", channel_id="1", message_id="11")).content)

    async def test_threads_and_rename(self):
        self.assertIn("開了討論串 每日報告（4）", (await self.call(action="create_thread", channel_id="1", name="每日報告")).content)
        self.assertIn("#general 改名成 #大廳", (await self.call(action="rename", channel_id="1", name="大廳")).content)
        self.assertIn("私訊沒有名稱", (await self.call(action="rename", channel_id="77", name="x")).content)


if __name__ == "__main__":
    unittest.main()
