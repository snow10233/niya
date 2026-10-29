import getpass
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path

import httpx

import niya

from niya.agent.claude_code import ClaudeCodeAgent
from niya.agent.context import Conversation
from niya.agent.loop import Agent
from niya.agent.prompts import build_system_prompt, load_persona
from niya.alarms import Alarms
from niya.agent.sanitize import neutralize, wrap_untrusted
from niya.clock import Clock
from niya.config import Settings
from niya.db import Database
from niya.llm import OpenRouterClient, db_recorder
from niya.model_choice import CLAUDE, ModelChoice, current_effort
from niya.redact import Redactor
from niya.tools.alarm_tool import alarm_tools
from niya.tools.egress import EgressDetector, EgressGate, Notifier
from niya.tools.files import file_tools
from niya.tools.hardline import default_hardline
from niya.tools.memory_tools import memory_tools
from niya.tools.processes import BackgroundProcess, ProcessRegistry
from niya.tools.protected_paths import ProtectedPaths
from niya.tools.registry import OutputFilter, Tool, ToolContext, ToolOutcome, ToolRegistry
from niya.tools.shell import ShellSession
from niya.tools.terminal import egress_guard, hardline_guard, terminal_tools
from niya.tools.url_safety import set_allow_private_urls
from niya.tools.web import web_tools
from niya.tools.web_providers import Brave, ExtractProvider, NativeExtractor, SearchProvider, SearXNG, Tavily

CODE_DIR = Path(niya.__file__).resolve().parents[2]


FEATURES = ("recall", "alarm")


def system_prompt_factory(settings: Settings, *, channel: str, features: Collection[str] = FEATURES) -> Callable[[], str]:
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
    egress: EgressGate
    alarms: Alarms
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


def build_core_tools(
    settings: Settings,
    db: Database,
    clock: Clock,
    *,
    notify_owner: Notifier | None = None,
    on_process_exit: Callable[[BackgroundProcess], None] | None = None,
) -> CoreTools:
    shell = ShellSession(settings.home / "cache" / "shell", secret_names=settings.secrets.keys())
    processes = ProcessRegistry(settings.home / "processes", now=clock.now, on_exit=on_process_exit)
    detector = EgressDetector()
    gate = EgressGate(db, now=clock.now, detector=detector, allow_hosts=settings.egress_allow_hosts, taint_scope=settings.egress_taint_scope, notify=notify_owner)
    guards = [hardline_guard(default_hardline(protected_dirs(settings))), egress_guard(gate, lambda: shell.cwd)]
    alarms = Alarms(db, clock, settings.tz)
    http = httpx.AsyncClient(timeout=60)
    search, extract = web_providers(settings, http)
    tools = [
        *terminal_tools(shell, processes, guards, is_fetch=detector.fetches_network),
        *file_tools(lambda: shell.cwd, ProtectedPaths(data_dirs(settings))),
        *web_tools(search, extract, secrets=settings.secrets.values),
        *memory_tools(db, settings.tz),
        *alarm_tools(alarms),
    ]

    def taint(ctx: ToolContext, outcome: ToolOutcome) -> None:
        if outcome.external_source:
            gate.mark_tainted(ctx.session_id, ctx.turn_id, outcome.external_source)

    registry = ToolRegistry(tools, output_filters=output_filters(Redactor(settings.secrets.values())), on_outcome=taint)
    clients = [http] + ([extract.http] if isinstance(extract, NativeExtractor) else [])
    return CoreTools(registry=registry, shell=shell, processes=processes, egress=gate, alarms=alarms, clients=clients)


AnyAgent = Agent | ClaudeCodeAgent


def process_notice(bg: BackgroundProcess) -> str:
    """What niya hears when a background job it asked to be told about finishes (no output: that goes through the tools)."""
    return f"背景程序 {bg.id} 結束了（exit {bg.exit_code}）：{bg.command}\n需要的話用 process 工具看輸出。"


def build_agent(settings: Settings, db: Database, clock: Clock, tools: CoreTools, conversation: Conversation, choice: ModelChoice) -> AnyAgent:
    effort = current_effort(db, settings)[0] or None
    if choice.provider == CLAUDE:
        return ClaudeCodeAgent(
            model=choice.model,
            tools=tools.registry,
            conversation=conversation,
            clock=clock,
            config_dir=settings.claude_config_dir,
            cwd=settings.home,
            max_iterations=settings.max_iterations,
            max_context_tokens=settings.max_context_tokens,
            effort=effort,
            secrets=settings.secrets,
            on_turn_end=tools.egress.end_turn,
            recorder=db_recorder(db, clock),
        )
    return Agent(
        llm=OpenRouterClient(settings.api_key, base_url=settings.base_url, recorder=db_recorder(db, clock)),
        model=choice.model,
        tools=tools.registry,
        conversation=conversation,
        clock=clock,
        max_iterations=settings.max_iterations,
        max_context_tokens=settings.max_context_tokens,
        effort=effort,
        on_turn_end=tools.egress.end_turn,
    )


def needs_new_session(conversation: Conversation, choice: ModelChoice) -> bool:
    """History made on one engine cannot be handed to another, so switching provider starts a new session."""
    return conversation.engine is not None and conversation.engine != choice.provider and bool(conversation.messages)
