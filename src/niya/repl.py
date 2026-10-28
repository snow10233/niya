import asyncio

from niya.agent.loop import Agent
from niya.clock import RealClock
from niya.config import Settings
from niya.db import Database
from niya.inbox import InboundItem
from niya.llm import LLMError, OpenRouterClient, db_recorder
from niya.runtime import build_core_tools, open_conversation, restart_conversation, system_prompt_factory
from niya.tools.processes import BackgroundProcess

HELP = "指令：/new 開新 session、/egress list|allow <host>|remove <host>、/quit 離開"


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


async def run_repl(settings: Settings) -> int:
    db = Database(settings.db_path)
    clock = RealClock()
    llm = OpenRouterClient(settings.api_key, base_url=settings.base_url, recorder=db_recorder(db, clock))
    prompt = system_prompt_factory(settings, channel="主人的終端機")
    tools = build_core_tools(settings, db, clock, notify_owner=_notify, on_process_exit=_announce_exit)
    conv = open_conversation(db, settings, clock, prompt)
    agent = Agent(
        llm=llm,
        model=settings.model,
        tools=tools.registry,
        conversation=conv,
        clock=clock,
        max_iterations=settings.max_iterations,
        on_turn_end=tools.egress.end_turn,
    )
    print(f"niya REPL（session {conv.session_id}）。{HELP}")
    try:
        while True:
            try:
                line = (await asyncio.to_thread(input, "you> ")).strip()
            except EOFError:
                break
            if not line:
                continue
            if line == "/quit":
                break
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
        await tools.close()
        await llm.aclose()
        db.close()
    return 0
