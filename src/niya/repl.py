import asyncio

from niya.brain import Brain
from niya.clock import RealClock
from niya.config import Settings
from niya.control import ModelControl
from niya.db import Database
from niya.inbox import Inbox
from niya.model_choice import current_choice
from niya.runtime import AnyAgent, build_agent, build_core_tools, needs_new_session, open_conversation, process_notice, restart_conversation, system_prompt_factory

HELP = "指令：/new 開新 session、/model 查看或切換模型、/effort 調整思考強度、/alarms [cancel <id>] 鬧鐘、/egress list|allow <host>|remove <host>、/exit 離開"


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


async def run_repl(settings: Settings) -> int:
    db = Database(settings.db_path)
    clock = RealClock()
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

        async def context_full(reason: str, used: int) -> None:
            agent = control.agent
            agent.conversation = restart_conversation(agent.conversation, settings, clock, prompt, reason=reason)
            print(f"\n（context 用了 {used}%，先開新 session {agent.conversation.session_id}。之前的對話都在資料庫裡，niya 用 deep_recall 想得起來。）")

        brain = Brain(inbox=inbox, control=control, egress=tools.egress, reply=_say, report=_report, on_context_full=context_full)
        tasks = [asyncio.create_task(brain.run()), asyncio.create_task(tools.alarms.run(inbox))]
        print(f"niya REPL（session {agent.conversation.session_id}，{control.status()}）。\n{HELP}")
        if inbox.pending():
            print(f"（還有 {len(inbox.pending())} 則沒處理完的訊息，先交給 niya）")
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
            if command in ("/model", "/effort", "/new"):
                async with brain.lock:
                    if command == "/model":
                        print(await control.model(arg))
                    elif command == "/effort":
                        print(await control.effort(arg))
                    else:
                        agent = control.agent
                        agent.conversation = restart_conversation(agent.conversation, settings, clock, prompt, reason="manual")
                        print(f"已開新 session：{agent.conversation.session_id}")
                continue
            if command == "/alarms":
                print(_alarms_command(tools.alarms, arg))
                continue
            if command == "/egress":
                print(_egress_command(tools.egress, line))
                continue
            inbox.put("owner", line)
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
