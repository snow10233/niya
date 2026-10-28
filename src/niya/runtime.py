import getpass
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path

import httpx

import niya

from niya.agent.context import Conversation
from niya.agent.prompts import build_system_prompt, load_persona
from niya.agent.sanitize import neutralize, wrap_untrusted
from niya.clock import Clock
from niya.config import Settings
from niya.db import Database
from niya.redact import Redactor
from niya.tools.files import file_tools
from niya.tools.hardline import default_hardline
from niya.tools.processes import BackgroundProcess, ProcessRegistry
from niya.tools.protected_paths import ProtectedPaths
from niya.tools.registry import OutputFilter, Tool, ToolRegistry
from niya.tools.shell import ShellSession
from niya.tools.terminal import hardline_guard, terminal_tools
from niya.tools.url_safety import set_allow_private_urls
from niya.tools.web import web_tools
from niya.tools.web_providers import Brave, ExtractProvider, NativeExtractor, SearchProvider, SearXNG, Tavily

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
    clients: list[httpx.AsyncClient] = field(default_factory=list)

    async def close(self) -> None:
        await self.processes.shutdown()
        self.shell.close()
        for client in self.clients:
            await client.aclose()


def protected_dirs(settings: Settings) -> list[Path]:
    """Directories the terminal may not delete; the file tools additionally leave data and config alone."""
    return [settings.home, Path("/etc/niya"), CODE_DIR]


def data_dirs(settings: Settings) -> list[Path]:
    return [settings.home, Path("/etc/niya")]


def output_filters(redactor: Redactor) -> list[OutputFilter]:
    def redact(text: str, tool: Tool, external: bool) -> str:
        return redactor(text, file_content=tool.output_kind == "file")

    def sanitize(text: str, tool: Tool, external: bool) -> str:
        return wrap_untrusted(text, tool.name) if external else neutralize(text)

    return [redact, sanitize]


def web_providers(settings: Settings, http: httpx.AsyncClient) -> tuple[SearchProvider | None, ExtractProvider]:
    set_allow_private_urls(settings.allow_private_urls)
    native = NativeExtractor()
    if key := settings.secrets.get("TAVILY_API_KEY"):
        tavily = Tavily(key, http)
        return tavily, tavily
    if key := settings.secrets.get("BRAVE_SEARCH_API_KEY"):
        return Brave(key, http), native
    if settings.searxng_url:
        return SearXNG(settings.searxng_url, http), native
    return None, native


def build_core_tools(settings: Settings, clock: Clock, *, on_process_exit: Callable[[BackgroundProcess], None] | None = None) -> CoreTools:
    shell = ShellSession(settings.home / "cache" / "shell", secret_names=settings.secrets.keys())
    processes = ProcessRegistry(settings.home / "processes", now=clock.now, on_exit=on_process_exit)
    guards = [hardline_guard(default_hardline(protected_dirs(settings)))]
    http = httpx.AsyncClient(timeout=60)
    search, extract = web_providers(settings, http)
    tools = [
        *terminal_tools(shell, processes, guards),
        *file_tools(lambda: shell.cwd, ProtectedPaths(data_dirs(settings))),
        *web_tools(search, extract, secrets=settings.secrets.values),
    ]
    registry = ToolRegistry(tools, output_filters=output_filters(Redactor(settings.secrets.values())))
    clients = [http] + ([extract.http] if isinstance(extract, NativeExtractor) else [])
    return CoreTools(registry=registry, shell=shell, processes=processes, clients=clients)
