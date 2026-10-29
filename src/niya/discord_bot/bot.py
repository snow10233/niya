"""The Discord face of niya, run as the long-lived service (`niya run`).

Listens only to the owner, in DMs or the channels set aside for niya. Everything sent is redacted first,
split to Discord's 2000-character limit without breaking code blocks, or attached as a .md file when long.
"""

import asyncio
import inspect
import io
import logging
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import app_commands

from niya.app.niya import Interface, Niya
from niya.core.clock import RealClock
from niya.core.config import Settings
from niya.core.redact import Redactor
from niya.discord_bot.progress import ProgressBubble
from niya.discord_bot.text import FILE_THRESHOLD, SleepReplyLimiter, chunk_message, should_handle

log = logging.getLogger(__name__)

PRESENCE_CHECK_S = 20
NOT_OWNER = "這個 bot 只聽主人的。"


class NiyaDiscord:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.client)
        self.redact = Redactor(settings.secrets.values())
        self.progress = ProgressBubble(self._target)
        self.niya = Niya(
            settings, Interface(channel="Discord", say=self.send, notice=self.send, confirm=self._confirm, on_busy=self._busy, progress=self.progress.add)
        )
        self.channel: Any = None  # where the owner last talked; replies go there
        self.limiter = SleepReplyLimiter()
        self._typing: asyncio.Task | None = None
        self._idle = asyncio.Event()
        self._presence_asleep: bool | None = None
        self._background: list[asyncio.Task] = []
        self.client.setup_hook = self._setup
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self._register_commands()

    # --- lifecycle ---------------------------------------------------------------------------------

    async def _setup(self) -> None:
        notes = await self.niya.start()
        guild = discord.Object(id=self.settings.discord_guild_id) if self.settings.discord_guild_id else None
        if guild is not None:
            self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self._background.append(asyncio.create_task(self._presence_loop()))
        for note in notes:
            log.info("startup: %s", note)

    async def on_ready(self) -> None:
        log.info("Discord ready as %s", self.client.user)

    async def run(self, token: str) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.client.close()))
        try:
            async with self.client:
                await self.client.start(token)
        finally:
            for task in self._background:
                task.cancel()
            await self.niya.close()

    # --- sending -----------------------------------------------------------------------------------

    async def _target(self) -> Any:
        if self.channel is None:
            user = await self.client.fetch_user(self.settings.discord_owner_id)
            self.channel = await user.create_dm()
        return self.channel

    async def send(self, text: str) -> None:
        text = self.redact(text)
        if not text.strip():
            return
        await self.progress.flush()
        channel = await self._target()
        if len(text) > FILE_THRESHOLD:
            await channel.send("內容比較長，放在附檔：", file=discord.File(io.BytesIO(text.encode("utf-8")), filename="niya.md"))
            return
        for chunk in chunk_message(text):
            await channel.send(chunk)

    async def _respond(self, interaction: discord.Interaction, text: str) -> None:
        chunks = chunk_message(self.redact(text) or "（沒有內容）")
        if interaction.response.is_done():
            for chunk in chunks:
                await interaction.followup.send(chunk)
            return
        await interaction.response.send_message(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    async def _busy(self, busy: bool) -> None:
        """Show "typing…" in the owner's channel while a turn runs."""
        if busy:
            self._idle.clear()
            channel = await self._target()

            async def keep_typing() -> None:
                async with channel.typing():
                    await self._idle.wait()

            self._typing = asyncio.create_task(keep_typing())
        else:
            self._idle.set()
            if self._typing is not None:
                await asyncio.gather(self._typing, return_exceptions=True)
                self._typing = None
            await self.progress.finish()

    async def _confirm(self, question: str) -> bool:
        # A /model command typed by the owner already is the confirmation; say what it costs.
        await self.send(f"（{question.replace('確定嗎？', '').strip()}）")
        return True

    async def _presence_loop(self) -> None:
        await self.client.wait_until_ready()
        while True:
            asleep = self.niya.asleep
            if asleep != self._presence_asleep:
                if asleep:
                    await self.client.change_presence(status=discord.Status.idle, activity=discord.CustomActivity(name="💤 睡覺中"))
                else:
                    await self.client.change_presence(status=discord.Status.online, activity=None)
                self._presence_asleep = asleep
            await asyncio.sleep(PRESENCE_CHECK_S)

    # --- receiving ---------------------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        is_dm = isinstance(message.channel, discord.DMChannel)
        if not should_handle(
            author_id=message.author.id,
            author_is_bot=message.author.bot,
            owner_id=self.settings.discord_owner_id,
            is_dm=is_dm,
            channel_id=message.channel.id,
            channel_ids=self.settings.discord_channel_ids,
        ):
            return
        self.channel = message.channel
        text = message.content.strip()
        if text == "!wake":
            await message.channel.send(await self.niya.wake())
            return
        attachments = [f"[附件] {a.filename}：{a.url}" for a in message.attachments]
        text = "\n".join([text, *attachments]).strip()
        if not text:
            return
        note = self.niya.owner_says(text, meta={"channel_id": message.channel.id, "message_id": message.id})
        if note and self.limiter.allow(RealClock().now()):
            await message.channel.send("睡覺中（整理完就醒），急事請 /wake 或 !wake。")

    async def _run(self, interaction: discord.Interaction, action: Callable[[], Awaitable[str] | str]) -> None:
        """Every slash command: owner only, then show what niya says."""
        if interaction.user.id != self.settings.discord_owner_id:
            await interaction.response.send_message(NOT_OWNER, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        self.channel = interaction.channel
        result = action()
        await self._respond(interaction, await result if inspect.isawaitable(result) else result)

    def _register_commands(self) -> None:
        niya, tree = self.niya, self.tree

        @tree.command(name="status", description="niya 現在的狀態")
        async def status(interaction: discord.Interaction) -> None:
            await self._run(interaction, niya.status)

        @tree.command(name="wake", description="叫醒睡覺中的 niya")
        async def wake(interaction: discord.Interaction) -> None:
            await self._run(interaction, niya.wake)

        @tree.command(name="stop", description="停下 niya 正在做的事")
        async def stop(interaction: discord.Interaction) -> None:
            await self._run(interaction, niya.stop)

        @tree.command(name="sleep", description="讓 niya 現在去睡，整理記憶")
        @app_commands.describe(kind="不選就自動判斷")
        @app_commands.choices(kind=[app_commands.Choice(name="午睡", value="nap"), app_commands.Choice(name="夜間睡眠", value="night")])
        async def sleep(interaction: discord.Interaction, kind: str = "") -> None:
            await self._run(interaction, lambda: niya.go_to_sleep(kind))

        @tree.command(name="dream", description="看最近一次的夢境日誌")
        async def dream(interaction: discord.Interaction) -> None:
            await self._run(interaction, niya.dream)

        @tree.command(name="alarms", description="看鬧鐘，或輸入 cancel <id> 取消")
        async def alarms(interaction: discord.Interaction, args: str = "") -> None:
            await self._run(interaction, lambda: niya.alarms(args))

        @tree.command(name="egress", description="外傳的已知主機：list、allow <host>、remove <host>")
        async def egress(interaction: discord.Interaction, args: str = "list") -> None:
            await self._run(interaction, lambda: niya.egress(args))

        @tree.command(name="model", description="查看或切換模型：sonnet、openrouter:<模型 ID>、search <關鍵字>、reset")
        async def model(interaction: discord.Interaction, choice: str = "") -> None:
            await self._run(interaction, lambda: niya.model(choice))

        @tree.command(name="effort", description="查看或調整思考強度：low / medium / high / xhigh / max / reset")
        async def effort(interaction: discord.Interaction, level: str = "") -> None:
            await self._run(interaction, lambda: niya.effort(level))

        @tree.command(name="new", description="開新 session（清空對話，不經過睡眠整理）")
        async def new(interaction: discord.Interaction) -> None:
            await self._run(interaction, niya.new_session)


def run_discord(settings: Settings) -> int:
    token = settings.secrets.get("DISCORD_TOKEN")
    if not token or not settings.discord_owner_id:
        print("無法啟動：請設定 DISCORD_TOKEN 和主人的 Discord 使用者 ID（[discord] owner_id 或 NIYA_DISCORD_OWNER_ID）。")
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(NiyaDiscord(settings).run(token))
    return 0
