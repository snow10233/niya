import asyncio
import re
from datetime import datetime

from niya.agent.budget import percent
from niya.agent.render import format_ts
from niya.app.brain import Brain
from niya.app.control import ModelControl
from niya.app.runtime import (
    AnyAgent,
    build_agent,
    build_core_tools,
    needs_new_session,
    open_conversation,
    process_notice,
    restart_conversation,
    sleep_completer,
    system_prompt_factory,
)
from niya.core.clock import Clock, FakeClock, RealClock
from niya.core.config import Settings
from niya.core.db import Database
from niya.core.inbox import Inbox
from niya.llm.choice import current_choice
from niya.sleep.orchestrator import KIND_LABELS, SleepOrchestrator

HELP = (
    "指令：/status 狀態、/sleep [nap|night] 去睡、/wake 叫醒、/dream 夢境日誌、/new 開新 session、/model 模型、/effort 思考強度、"
    "/alarms [cancel <id>] 鬧鐘、/egress list|allow|remove 外傳主機、/exit 離開"
)
FAKE_HELP = "（快轉時鐘模式：/advance 30m、/advance 2h、/advance 1d 讓時間往前走）"
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([mhd])$")


async def _notify(text: str) -> None:
    print(f"\n{text}")


def _alarms_command(alarms, arg: str) -> str:
    parts = arg.split()
    if not parts:
        active = alarms.active()
        return "\n".join(alarms.describe(a) for a in active) if active else "目前沒有鬧鐘。"
    if len(parts) == 2 and parts[0] == "cancel" and parts[1].lstrip("#").isdigit():
        try:
            cancelled = alarms.cancel(int(parts[1].lstrip("#")))
        except ValueError as exc:
            return str(exc)
        return f"已取消 #{cancelled.id}「{cancelled.title}」"
    return "用法：/alarms | /alarms cancel <id>"


def _egress_command(gate, line: str) -> str:
    parts = line.split()
    if len(parts) == 2 and parts[1] == "list":
        return "\n".join(f"{host}（{source}）" for host, source in gate.known_hosts()) or "目前沒有已知主機。"
    if len(parts) == 3 and parts[1] == "allow":
        gate.allow(parts[2])
        return f"已加入已知主機：{parts[2]}"
    if len(parts) == 3 and parts[1] == "remove":
        return f"已移除：{parts[2]}" if gate.remove(parts[2]) else f"清單裡沒有 {parts[2]}"
    return "用法：/egress list | /egress allow <host> | /egress remove <host>"


async def _confirm(question: str) -> bool:
    answer = await asyncio.to_thread(input, f"{question} [y/N] ")
    return answer.strip().lower() in ("y", "yes")


async def _say(text: str) -> None:
    print(f"\nniya> {text}")


async def _report(text: str) -> None:
    print(f"\n{text}")


def _status(control: ModelControl, orchestrator: SleepOrchestrator, clock: Clock, db: Database, alarms) -> str:
    agent = control.agent
    now = clock.now()
    lines = [
        f"現在 {format_ts(now, agent.conversation.tz)}，session {agent.conversation.session_id}",
        control.status(),
        f"context：{agent.last_prompt_tokens:,} / {agent.context_limit:,} tokens（{percent(agent.last_prompt_tokens, agent.context_limit)}%）",
    ]
    if orchestrator.asleep:
        stage = db.one("SELECT kind, stage FROM sleep_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1")
        lines.append(f"睡覺中（{KIND_LABELS.get(stage['kind'], stage['kind'])}，{stage['stage']}）" if stage else "睡覺中")
    else:
        lines.append(f"醒著；下一次睡覺：{orchestrator.next_slot(now)}")
        if db.get_meta("sleep.resume"):
            lines.append("上次的睡眠還沒整理完，閒下來會續睡。")
    lines.append(f"鬧鐘 {len(alarms.active())} 個；長期記憶 {db.one("SELECT COUNT(*) AS n FROM memories WHERE state IN ('labile', 'active')")['n']} 條")
    return "\n".join(lines)


def _dream(db: Database, tz) -> str:
    row = db.one("SELECT created_at, content FROM dream_journal ORDER BY id DESC LIMIT 1")
    return f"{format_ts(row['created_at'], tz)} 的夢：\n{row['content']}" if row else "還沒有夢境日誌（夜間睡眠後才會有）。"


async def run_repl(settings: Settings, *, fake_clock: str | None = None) -> int:
    db = Database(settings.db_path)
    clock: Clock = RealClock()
    if fake_clock is not None:
        start = datetime.strptime(fake_clock, "%Y-%m-%d %H:%M").replace(tzinfo=settings.tz).timestamp() if fake_clock else RealClock().now()
        clock = FakeClock(start)
    inbox = Inbox(db, clock)
    tools = build_core_tools(settings, db, clock, notify_owner=_notify, on_process_exit=lambda bg: inbox.put("process", process_notice(bg)))
    prompt = system_prompt_factory(settings, channel="主人的終端機", core_block=tools.memory.core_block)
    choice = current_choice(db, settings)[0]
    conv = open_conversation(db, settings, clock, prompt)
    control: ModelControl | None = None
    tasks: list[asyncio.Task] = []
    try:
        agent: AnyAgent = build_agent(settings, db, clock, tools, conv, choice)
        if needs_new_session(conv, choice):
            agent.conversation = restart_conversation(conv, settings, clock, prompt, reason="engine_switch")
            print(f"模型換成 {choice.provider}，已開新 session。")
        control = ModelControl(settings=settings, db=db, clock=clock, tools=tools, prompt=prompt, agent=agent, confirm=_confirm)
        orchestrator: SleepOrchestrator | None = None

        async def context_full(used: int, progress: str) -> None:
            print(f"\nniya> 有點累了，先睡一下，整理完就醒。（context 用了 {used}%）")
            await orchestrator.request_sleep("overflow", progress=progress)

        brain = Brain(inbox=inbox, control=control, egress=tools.egress, reply=_say, report=_report, on_context_full=context_full)

        async def wake_up(kind: str, reason: str, line: str) -> None:
            current = control.agent
            current.conversation = restart_conversation(current.conversation, settings, clock, prompt, reason="sleep")
            brain.carry.append(line)
            print(f"\n（niya {line}）")

        orchestrator = SleepOrchestrator(
            db=db,
            clock=clock,
            tz=settings.tz,
            engine=tools.memory,
            completer=sleep_completer(settings, db, clock, tools),
            brain=brain,
            current_session=lambda: control.agent.conversation.session_id,
            wake_up=wake_up,
            notify=_notify,
            backups_dir=settings.home / "backups",
            budget_usd=settings.sleep_budget_usd,
        )

        async def alarm_rang(fired) -> None:
            if orchestrator.asleep and any(alarm.wake_from_sleep for alarm in fired):
                print("\n（鬧鐘響了，叫醒 niya）")
                await orchestrator.wake("alarm")

        tasks = [
            asyncio.create_task(brain.run()),
            asyncio.create_task(tools.alarms.run(inbox, on_fire=alarm_rang)),
            asyncio.create_task(orchestrator.run()),
        ]
        print(f"niya REPL（session {control.agent.conversation.session_id}，{control.status()}）。\n{HELP}")
        if isinstance(clock, FakeClock):
            print(f"{FAKE_HELP}現在是 {format_ts(clock.now(), settings.tz)}。")
        if inbox.pending():
            print(f"（還有 {len(inbox.pending())} 則沒處理完的訊息，先交給 niya）")
            await brain.idle()

        async def settle() -> None:
            await asyncio.sleep(0)
            while orchestrator.asleep:
                await asyncio.sleep(0.05)
            await brain.idle()

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
            if command == "/status":
                print(_status(control, orchestrator, clock, db, tools.alarms))
                continue
            if command == "/dream":
                print(_dream(db, settings.tz))
                continue
            if command == "/sleep":
                if arg and arg not in KIND_LABELS:
                    print("用法：/sleep、/sleep nap、/sleep night")
                    continue
                print(await orchestrator.request_sleep("manual", kind=arg or None))
                continue
            if command == "/wake":
                print("叫醒了，還沒整理完的部分之後會找空檔續睡。" if await orchestrator.wake("owner") else "niya 醒著。")
                continue
            if command == "/wait":
                await settle()
                continue
            if command == "/advance":
                match = _DURATION.match(arg)
                if not isinstance(clock, FakeClock) or not match:
                    print("只有 --fake-clock 模式能用：/advance 30m、/advance 2h、/advance 1d")
                    continue
                clock.advance(float(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2)])
                print(f"現在是 {format_ts(clock.now(), settings.tz)}")
                fired = tools.alarms.fire_due(inbox, clock.now())
                if fired:
                    await alarm_rang([tools.alarms.get(i) for i in fired])
                await orchestrator.tick()
                await settle()
                continue
            if command in ("/model", "/effort", "/new"):
                if orchestrator.asleep:
                    print("niya 睡覺中，醒來再改（或先 /wake）。")
                    continue
                async with brain.lock:
                    if command == "/model":
                        print(await control.model(arg))
                    elif command == "/effort":
                        print(await control.effort(arg))
                    else:
                        current = control.agent
                        current.conversation = restart_conversation(current.conversation, settings, clock, prompt, reason="manual")
                        print(f"已開新 session：{current.conversation.session_id}")
                continue
            if command == "/alarms":
                print(_alarms_command(tools.alarms, arg))
                continue
            if command == "/egress":
                print(_egress_command(tools.egress, line))
                continue
            inbox.put("owner", line)
            if orchestrator.asleep:
                print("（niya 睡覺中，整理完就醒，訊息先排隊；急事用 /wake）")
                continue
            await brain.idle()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if control is not None:
            await control.agent.aclose()
        await tools.close()
        db.close()
    return 0
