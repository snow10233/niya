import getpass
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

import niya

from niya.agent.context import Conversation
from niya.agent.prompts import build_system_prompt, load_persona
from niya.clock import Clock
from niya.config import Settings
from niya.db import Database
from niya.tools.hardline import default_hardline
from niya.tools.processes import BackgroundProcess, ProcessRegistry
from niya.tools.registry import ToolRegistry
from niya.tools.shell import ShellSession
from niya.tools.terminal import hardline_guard, terminal_tools

CODE_DIR = Path(niya.__file__).resolve().parents[2]


def system_prompt_factory(settings: Settings, *, channel: str, features: Collection[str] = ()) -> Callable[[], str]:
    def build() -> str:
        return build_system_prompt(
            persona=load_persona(settings.persona_path),
            timezone=settings.timezone,
            channel=channel,
            run_as=getpass.getuser(),
            features=features,
        )

    return build


def open_conversation(db: Database, settings: Settings, clock: Clock, prompt: Callable[[], str]) -> Conversation:
    return Conversation.open_current(db, now=clock.now(), tz=settings.tz, system_prompt=prompt)


def restart_conversation(conv: Conversation, settings: Settings, clock: Clock, prompt: Callable[[], str], *, reason: str) -> Conversation:
    conv.end(now=clock.now(), reason=reason)
    return Conversation.start_new(conv.db, now=clock.now(), tz=settings.tz, system_prompt=prompt())


@dataclass
class CoreTools:
    registry: ToolRegistry
    shell: ShellSession
    processes: ProcessRegistry

    async def close(self) -> None:
        await self.processes.shutdown()
        self.shell.close()


def protected_dirs(settings: Settings) -> list[Path]:
    return [settings.home, Path("/etc/niya"), CODE_DIR]


def build_core_tools(settings: Settings, clock: Clock, *, on_process_exit: Callable[[BackgroundProcess], None] | None = None) -> CoreTools:
    shell = ShellSession(settings.home / "cache" / "shell", secret_names=settings.secrets.keys())
    processes = ProcessRegistry(settings.home / "processes", now=clock.now, on_exit=on_process_exit)
    guards = [hardline_guard(default_hardline(protected_dirs(settings)))]
    return CoreTools(registry=ToolRegistry(terminal_tools(shell, processes, guards)), shell=shell, processes=processes)
