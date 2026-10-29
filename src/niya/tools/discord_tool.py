"""discord: read and post in the owner's Discord server.

Goes through the bot's REST API rather than the gateway connection, so it also works from the REPL. Posting
is limited to the configured server (or the bot's only one) and the owner's DMs; messages read from channels
count as external content, since anyone (or any feed) in the channel may have written them.
"""

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from niya.agent.render import format_ts
from niya.discord_bot.text import chunk_message
from niya.tools.egress import EgressGate
from niya.tools.registry import Tool, ToolContext, ToolError, ToolText

API = "https://discord.com/api/v10"
ACTIONS = ("list_channels", "read", "send", "delete", "create_thread", "rename")
MAX_FILES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024  # Discord's upload limit for bots without boosts
MAX_READ = 100
EMBED_CHARS = 300
CHANNEL_TYPES = {0: "文字", 2: "語音", 4: "分類", 5: "公告", 10: "討論串", 11: "討論串", 12: "私人討論串", 13: "舞台", 15: "論壇", 16: "媒體"}
THREAD_TYPES = (10, 11, 12)

DESCRIPTION = (
    "在主人的 Discord 伺服器讀訊息、發訊息。action：list_channels（列出頻道和進行中的討論串）、read（讀頻道或討論串最近的訊息）、"
    "send（發訊息，可附檔，例如你生的圖或語音；不填 channel_id 就發到主人現在跟你說話的地方）、delete（刪掉你自己發的訊息）、"
    "create_thread（開討論串）、rename（改頻道或討論串名稱）。"
    "平常回主人直接回覆就好，不用這個工具；要附檔、發到別的頻道或看頻道裡的歷史時才用。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "channel_id": {"type": "string", "description": "頻道或討論串 ID"},
        "text": {"type": "string", "description": "send：訊息內容"},
        "files": {"type": "array", "items": {"type": "string"}, "description": f"send：要附上的本機檔案（最多 {MAX_FILES} 個，每個 10 MB 以內）"},
        "reply_to": {"type": "string", "description": "send：回覆哪一則訊息（ID）"},
        "message_id": {"type": "string", "description": "delete：要刪的訊息；create_thread：從哪一則訊息開討論串（不填就開獨立的討論串）"},
        "name": {"type": "string", "description": "create_thread、rename：名稱"},
        "limit": {"type": "integer", "description": f"read：幾則（預設 20，最多 {MAX_READ}）", "minimum": 1, "maximum": MAX_READ},
        "before": {"type": "string", "description": "read：只看這則訊息之前的，用來往前翻"},
    },
    "required": ["action"],
}


class DiscordRest:
    def __init__(self, token: str, http: httpx.AsyncClient, *, guild_id: int = 0, owner_id: int = 0) -> None:
        self.token = token
        self.http = http
        self.guild_id = guild_id
        self.owner_id = owner_id
        self._me: dict[str, Any] | None = None

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(2):
            response = await self.http.request(method, API + path, headers={"Authorization": f"Bot {self.token}"}, **kwargs)
            if response.status_code == 429 and attempt == 0:
                await asyncio.sleep(min(float(response.json().get("retry_after", 1)), 5.0))
                continue
            break
        if response.status_code == 204:
            return None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            message = data.get("message") if isinstance(data, dict) else None
            raise ToolError(f"Discord 回了 {response.status_code}：{message or response.text[:200]}")
        return data

    async def me(self) -> dict[str, Any]:
        if self._me is None:
            self._me = await self.call("GET", "/users/@me")
        return self._me

    async def guild(self) -> int:
        if self.guild_id:
            return self.guild_id
        guilds = await self.call("GET", "/users/@me/guilds")
        if len(guilds) != 1:
            raise ToolError(f"bot 在 {len(guilds)} 個伺服器裡，請在 niya.toml 設定 [discord] guild_id。")
        self.guild_id = int(guilds[0]["id"])
        return self.guild_id

    async def owner_dm(self) -> str:
        if not self.owner_id:
            raise ToolError("沒有設定主人的 Discord ID（[discord] owner_id），不知道要傳給誰。")
        return str((await self.call("POST", "/users/@me/channels", json={"recipient_id": str(self.owner_id)}))["id"])

    async def channel(self, channel_id: str) -> dict[str, Any]:
        """The channel, after checking it is in the owner's server or the owner's DM."""
        channel = await self.call("GET", f"/channels/{channel_id}")
        if channel.get("guild_id"):
            if int(channel["guild_id"]) != await self.guild():
                raise ToolError("這個頻道不在主人的伺服器裡。")
        elif not any(int(r["id"]) == self.owner_id for r in channel.get("recipients") or ()):
            raise ToolError("只能用私訊跟主人說話。")
        return channel


def _label(channel: dict[str, Any]) -> str:
    if channel.get("guild_id"):
        return f"#{channel.get('name')}"
    return "主人的私訊"


def _render(message: dict[str, Any], tz: ZoneInfo) -> str:
    when = format_ts(datetime.fromisoformat(message["timestamp"]).timestamp(), tz)
    author = message.get("author") or {}
    name = author.get("global_name") or author.get("username") or "?"
    lines = [f"[{when}] {name}{'（bot）' if author.get('bot') else ''}: {message.get('content') or ''}  (msg {message['id']})"]
    for attachment in message.get("attachments") or ():
        lines.append(f"  [附件] {attachment.get('filename')}：{attachment.get('url')}")
    for embed in message.get("embeds") or ():
        body = " ".join(str(embed.get(k) or "") for k in ("title", "description")).strip()
        if body or embed.get("url"):
            lines.append(f"  [嵌入] {body[:EMBED_CHARS]}" + (f" {embed['url']}" if embed.get("url") else ""))
    return "\n".join(lines)


def discord_tools(
    rest: DiscordRest,
    *,
    tz: ZoneInfo,
    current_channel: Callable[[], str | None],
    gate: EgressGate,
    redact: Callable[[str], str],
    free_dirs: Sequence[Path] = (),
    protected: Callable[[Path], str | None] = lambda path: None,
) -> list[Tool]:
    async def list_channels(args: dict[str, Any]) -> str:
        guild = await rest.guild()
        channels = await rest.call("GET", f"/guilds/{guild}/channels")
        threads = (await rest.call("GET", f"/guilds/{guild}/threads/active")).get("threads") or []
        by_parent: dict[str, list[dict[str, Any]]] = {}
        for item in [*channels, *threads]:
            by_parent.setdefault(str(item.get("parent_id") or ""), []).append(item)

        def lines(parent: str, depth: int) -> list[str]:
            out = []
            for item in sorted(by_parent.get(parent, []), key=lambda c: (c.get("type") == 4, c.get("position", 0), c.get("name", ""))):
                kind = CHANNEL_TYPES.get(item.get("type"), str(item.get("type")))
                out.append(f"{'  ' * depth}{item.get('name')}（{kind}，{item['id']}）")
                out += lines(str(item["id"]), depth + 1)
            return out

        return "\n".join(lines("", 0)) or "（沒有頻道）"

    async def read(args: dict[str, Any]) -> ToolText:
        channel_id = str(args.get("channel_id") or current_channel() or "")
        if not channel_id:
            raise ToolError("要給 channel_id。")
        channel = await rest.channel(channel_id)
        limit = min(max(int(args.get("limit") or 20), 1), MAX_READ)
        params: dict[str, Any] = {"limit": limit}
        if args.get("before"):
            params["before"] = str(args["before"])
        messages = await rest.call("GET", f"/channels/{channel_id}/messages", params=params)
        body = "\n".join(_render(m, tz) for m in reversed(messages)) or "（沒有訊息）"
        return ToolText(f"{_label(channel)} 最近 {len(messages)} 則：\n{body}", external=True, source="discord")

    async def send(args: dict[str, Any], ctx: ToolContext) -> str:
        text = redact(str(args.get("text") or "")).strip()
        paths = [Path(str(p)).expanduser() for p in args.get("files") or ()]
        if not text and not paths:
            raise ToolError("text 和 files 至少要有一個。")
        if len(paths) > MAX_FILES:
            raise ToolError(f"一次最多附 {MAX_FILES} 個檔案。")
        for path in paths:
            if not path.is_file():
                raise ToolError(f"找不到檔案：{path}")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ToolError(f"{path} 超過 10 MB，Discord 傳不了。")
            free = any(d.resolve() in path.resolve().parents for d in free_dirs)
            if not free and (reason := protected(path)):
                raise ToolError(f"不能傳：{reason}")
        channel_id = str(args.get("channel_id") or current_channel() or await rest.owner_dm())
        channel = await rest.channel(channel_id)
        outside = [str(p) for p in paths if not any(d.resolve() in p.resolve().parents for d in free_dirs)]
        if outside and (held := await gate.check_upload(f"discord send {' '.join(outside)} → {channel_id}", "discord.com", session_id=ctx.session_id, turn_id=ctx.turn_id)):
            return held
        chunks = chunk_message(text) if text else [""]
        sent = []
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"content": chunk, "allowed_mentions": {"parse": []}}
            if i == 0 and args.get("reply_to"):
                payload["message_reference"] = {"message_id": str(args["reply_to"]), "fail_if_not_exists": False}
            if i == len(chunks) - 1 and paths:
                files = {f"files[{n}]": (p.name, p.read_bytes()) for n, p in enumerate(paths)}
                message = await rest.call("POST", f"/channels/{channel_id}/messages", data={"payload_json": json.dumps(payload, ensure_ascii=False)}, files=files)
            else:
                message = await rest.call("POST", f"/channels/{channel_id}/messages", json=payload)
            sent.append(message["id"])
        attached = f"，附了 {len(paths)} 個檔案" if paths else ""
        return f"已傳到{_label(channel)}（msg {', '.join(sent)}{attached}）。"

    async def delete(args: dict[str, Any]) -> str:
        channel_id, message_id = str(args.get("channel_id") or ""), str(args.get("message_id") or "")
        if not channel_id or not message_id:
            raise ToolError("要給 channel_id 和 message_id。")
        await rest.channel(channel_id)
        message = await rest.call("GET", f"/channels/{channel_id}/messages/{message_id}")
        if (message.get("author") or {}).get("id") != (await rest.me())["id"]:
            raise ToolError("只能刪你自己發的訊息。")
        await rest.call("DELETE", f"/channels/{channel_id}/messages/{message_id}")
        return f"刪掉了 msg {message_id}。"

    async def create_thread(args: dict[str, Any]) -> str:
        channel_id, name = str(args.get("channel_id") or current_channel() or ""), str(args.get("name") or "").strip()
        if not channel_id or not name:
            raise ToolError("要給 channel_id 和 name。")
        await rest.channel(channel_id)
        if args.get("message_id"):
            thread = await rest.call("POST", f"/channels/{channel_id}/messages/{args['message_id']}/threads", json={"name": name[:100]})
        else:
            thread = await rest.call("POST", f"/channels/{channel_id}/threads", json={"name": name[:100], "type": 11})
        return f"開了討論串 {thread['name']}（{thread['id']}）。"

    async def rename(args: dict[str, Any]) -> str:
        channel_id, name = str(args.get("channel_id") or ""), str(args.get("name") or "").strip()
        if not channel_id or not name:
            raise ToolError("要給 channel_id 和 name。")
        before = await rest.channel(channel_id)
        if not before.get("guild_id"):
            raise ToolError("私訊沒有名稱可以改。")
        after = await rest.call("PATCH", f"/channels/{channel_id}", json={"name": name[:100]})
        return f"#{before.get('name')} 改名成 #{after.get('name')}。"

    async def discord(args: dict[str, Any], ctx: ToolContext) -> str | ToolText:
        action = args.get("action")
        if action == "list_channels":
            return await list_channels(args)
        if action == "read":
            return await read(args)
        if action == "send":
            return await send(args, ctx)
        if action == "delete":
            return await delete(args)
        if action == "create_thread":
            return await create_thread(args)
        if action == "rename":
            return await rename(args)
        raise ToolError(f"未知的 action：{action}（可用：{'、'.join(ACTIONS)}）")

    return [Tool(name="discord", description=DESCRIPTION, parameters=PARAMETERS, handler=discord, toolset="discord", timeout_s=120, max_result_chars=30_000)]
