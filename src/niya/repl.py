import asyncio

from niya.clock import RealClock
from niya.config import Settings
from niya.db import Database
from niya.inbox import InboundItem
from niya.control import ModelControl
from niya.llm import LLMError
from niya.model_choice import current_choice
from niya.runtime import AnyAgent, build_agent, build_core_tools, needs_new_session, open_conversation, restart_conversation, system_prompt_factory
from niya.tools.processes import BackgroundProcess

HELP = "指令：/new 開新 session、/model 查看或切換模型、/effort 調整思考強度、/egress list|allow <host>|remove <host>、/exit 離開"


async def _notify(text: str) -> None:
    print(f"\n{text}")


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


def _announce_exit(bg: BackgroundProcess) -> None:
    print(f"\n[背景程序 {bg.id} 結束，exit {bg.exit_code}] {bg.command}")


async def _confirm(question: str) -> bool:
    answer = await asyncio.to_thread(input, f"{question} [y/N] ")
    return answer.strip().lower() in ("y", "yes")


async def run_repl(settings: Settings) -> int:
    db = Database(settings.db_path)
    clock = RealClock()
    prompt = system_prompt_factory(settings, channel="主人的終端機")
    tools = build_core_tools(settings, db, clock, notify_owner=_notify, on_process_exit=_announce_exit)
    choice = current_choice(db, settings)[0]
    conv = open_conversation(db, settings, clock, prompt)
    control: ModelControl | None = None
    try:
        agent: AnyAgent = build_agent(settings, db, clock, tools, conv, choice)
        if needs_new_session(conv, choice):
            agent.conversation = restart_conversation(conv, settings, clock, prompt, reason="engine_switch")
            print(f"模型換成 {choice.provider}，已開新 session。")
        control = ModelControl(settings=settings, db=db, clock=clock, tools=tools, prompt=prompt, agent=agent, confirm=_confirm)
        print(f"niya REPL（session {agent.conversation.session_id}，{control.status()}）。\n{HELP}")

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
            if command == "/model":
                print(await control.model(arg))
                continue
            if command == "/effort":
                print(await control.effort(arg))
                continue
            agent = control.agent
            if line == "/new":
                agent.conversation = restart_conversation(agent.conversation, settings, clock, prompt, reason="manual")
                print(f"已開新 session：{agent.conversation.session_id}")
                continue
            if line.startswith("/egress"):
                print(_egress_command(tools.egress, line))
                continue
            now = clock.now()
            tools.egress.learn_owner_hosts(line)
            notes = tools.egress.resolve_owner_reply(agent.conversation.session_id, line)
            items = [InboundItem("system", note, now) for note in notes] + [InboundItem("owner", line, now)]
            try:
                result = await agent.run_turn(items)
            except LLMError as exc:
                print(f"[錯誤] {exc}")
                continue
            if not result.silent:
                print(f"niya> {result.text}")
    finally:
        if control is not None:
            await control.agent.aclose()
        await tools.close()
        db.close()
    return 0
