import asyncio

from niya.clock import RealClock
from niya.config import Settings
from niya.db import Database
from niya.inbox import InboundItem
from niya.llm import LLMError
from niya.model_choice import clear_choice, configured_choice, current_choice, describe_options, parse_choice, save_choice
from niya.runtime import AnyAgent, build_agent, build_core_tools, needs_new_session, open_conversation, restart_conversation, system_prompt_factory
from niya.tools.processes import BackgroundProcess

HELP = "指令：/new 開新 session、/model [模型|reset] 查看或切換模型、/egress list|allow <host>|remove <host>、/quit 離開"


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
    prompt = system_prompt_factory(settings, channel="主人的終端機")
    tools = build_core_tools(settings, db, clock, notify_owner=_notify, on_process_exit=_announce_exit)
    choice, source = current_choice(db, settings)
    conv = open_conversation(db, settings, clock, prompt)
    agent: AnyAgent | None = None
    try:
        agent = build_agent(settings, db, clock, tools, conv, choice)
        if needs_new_session(conv, choice):
            agent.conversation = restart_conversation(conv, settings, clock, prompt, reason="engine_switch")
            print(f"模型換成 {choice.provider}，已開新 session。")
        print(f"niya REPL（session {agent.conversation.session_id}，模型 {choice}，{source}）。{HELP}")

        async def switch_model(arg: str) -> None:
            nonlocal agent
            if not arg:
                now_choice, now_source = current_choice(db, settings)
                print(f"目前的模型：{now_choice}（{now_source}）\n{describe_options()}\n用法：/model <模型>、/model reset 改回設定檔")
                return
            if arg == "reset":
                new = configured_choice(settings)
            else:
                try:
                    new = parse_choice(arg)
                except ValueError as exc:
                    print(exc)
                    return
            conversation = agent.conversation
            if new.provider == agent.engine:
                await agent.set_model(new.model)
            else:
                try:
                    replacement = build_agent(settings, db, clock, tools, conversation, new)
                except ValueError as exc:
                    print(f"無法切換：{exc}")
                    return
                if needs_new_session(conversation, new):
                    answer = (await asyncio.to_thread(input, f"換到 {new.provider} 要開新 session，目前的對話會結束（紀錄留在資料庫）。確定嗎？[y/N] ")).strip().lower()
                    if answer not in ("y", "yes"):
                        await replacement.aclose()
                        print("沒有切換。")
                        return
                    replacement.conversation = restart_conversation(conversation, settings, clock, prompt, reason="engine_switch")
                await agent.aclose()
                agent = replacement
            if arg == "reset":
                clear_choice(db)
            else:
                save_choice(db, new)
            print(f"已切換到 {new}（session {agent.conversation.session_id}）")

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
            if line == "/model" or line.startswith("/model "):
                await switch_model(line.removeprefix("/model").strip())
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
        if agent is not None:
            await agent.aclose()
        await tools.close()
        db.close()
    return 0
