import asyncio
import re
from datetime import datetime

from niya.agent.render import format_ts
from niya.app.niya import Interface, Niya
from niya.core.clock import Clock, FakeClock, RealClock
from niya.core.config import Settings

HELP = (
    "指令：/status 狀態、/sleep [nap|night] 去睡、/wake 叫醒、/stop 停下目前的工作、/dream 夢境日誌、/new 開新 session、"
    "/model 模型、/effort 思考強度、/alarms [cancel <id>] 鬧鐘、/egress list|allow|remove 外傳主機、/exit 離開"
)
FAKE_HELP = "（快轉時鐘模式：/advance 30m、/advance 2h、/advance 1d 讓時間往前走）"
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([mhd])$")


async def _say(text: str) -> None:
    print(f"\nniya> {text}")


async def _notice(text: str) -> None:
    print(f"\n{text}")


async def _confirm(question: str) -> bool:
    answer = await asyncio.to_thread(input, f"{question} [y/N] ")
    return answer.strip().lower() in ("y", "yes")


async def run_repl(settings: Settings, *, fake_clock: str | None = None) -> int:
    clock: Clock = RealClock()
    if fake_clock is not None:
        start = datetime.strptime(fake_clock, "%Y-%m-%d %H:%M").replace(tzinfo=settings.tz).timestamp() if fake_clock else RealClock().now()
        clock = FakeClock(start)
    niya = Niya(settings, Interface(channel="主人的終端機", say=_say, notice=_notice, confirm=_confirm), clock=clock)
    try:
        notes = await niya.start()
        print(f"niya REPL（session {niya.control.agent.conversation.session_id}，{niya.control.status()}）。\n{HELP}")
        if isinstance(clock, FakeClock):
            print(f"{FAKE_HELP}現在是 {format_ts(clock.now(), settings.tz)}。")
        for note in notes:
            print(f"（{note}）")
        await niya.idle()
        while True:
            try:
                line = (await asyncio.to_thread(input, "you> ")).strip()
            except EOFError:
                break
            if not line:
                continue
            if line in ("/exit", "/quit"):
                break
            command, _, arg = line.partition(" ")
            arg = arg.strip()
            if command.startswith("/") and (reply := await _command(niya, command, arg)) is not None:
                if reply:
                    print(reply)
                continue
            if note := niya.owner_says(line):
                print(note)
                continue
            await niya.idle()
    finally:
        await niya.close()
    return 0


async def _command(niya: Niya, command: str, arg: str) -> str | None:
    """The reply to show, '' when there is nothing to print, None when it is not a command."""
    if command == "/status":
        return niya.status()
    if command == "/dream":
        return niya.dream()
    if command == "/sleep":
        return await niya.go_to_sleep(arg)
    if command == "/wake":
        return await niya.wake()
    if command == "/stop":
        return niya.stop()
    if command == "/model":
        return await niya.model(arg)
    if command == "/effort":
        return await niya.effort(arg)
    if command == "/new":
        return await niya.new_session()
    if command == "/alarms":
        return niya.alarms(arg)
    if command == "/egress":
        return niya.egress(arg)
    if command == "/wait":
        await niya.settle()
        return ""
    if command == "/advance":
        match = _DURATION.match(arg)
        if not match:
            return "用法：/advance 30m、/advance 2h、/advance 1d"
        reply = await niya.advance(float(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2)])
        print(reply)
        await niya.settle()
        return ""
    return None
