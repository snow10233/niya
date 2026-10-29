import getpass
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

import niya
from niya.agent.claude_code import ClaudeCodeAgent
from niya.agent.context import Conversation
from niya.agent.loop import Agent
from niya.agent.prompts import build_subagent_prompt, build_system_prompt, load_persona
from niya.agent.sanitize import neutralize, wrap_untrusted
from niya.app.alarms import Alarms
from niya.core.clock import Clock
from niya.core.config import Settings
from niya.core.db import Database
from niya.core.inbox import InboundItem
from niya.core.redact import Redactor
from niya.llm.choice import CLAUDE, ModelChoice, current_choice, current_effort, parse_choice
from niya.llm.openrouter import LLMError, OpenRouterClient, OpenRouterEmbedder, db_recorder
from niya.memory.engine import MemoryEngine, core_ids
from niya.skills.store import SkillStore
from niya.sleep.llm import ClaudeCodeCompleter, Completer, OpenRouterCompleter
from niya.tools.alarm_tool import alarm_tools
from niya.tools.delegate import delegate_tools
from niya.tools.egress import EgressDetector, EgressGate, Notifier
from niya.tools.files import file_tools
from niya.tools.hardline import default_hardline
from niya.tools.mcp_client import McpConnections
from niya.tools.memory_tools import memory_tools
from niya.tools.processes import BackgroundProcess, ProcessRegistry
from niya.tools.protected_paths import ProtectedPaths
from niya.tools.registry import OutputFilter, Tool, ToolContext, ToolOutcome, ToolRegistry
from niya.tools.shell import ShellSession
from niya.tools.skills import skill_tools
from niya.tools.terminal import egress_guard, hardline_guard, terminal_tools
from niya.tools.url_safety import set_allow_private_urls
from niya.tools.web import web_tools
from niya.tools.web_providers import Brave, ExtractProvider, NativeExtractor, SearchProvider, SearXNG, Tavily

CODE_DIR = Path(niya.__file__).resolve().parents[2]


FEATURES = ("memory", "alarm", "sleep", "skills")


def system_prompt_factory(
    settings: Settings,
    *,
    channel: str,
    features: Collection[str] = FEATURES,
    core_block: Callable[[], str] | None = None,
    skills_index: Callable[[], str] | None = None,
) -> Callable[[], str]:
    def build() -> str:
        return build_system_prompt(
            persona=load_persona(settings.persona_path),
            timezone=settings.timezone,
            channel=channel,
            run_as=getpass.getuser(),
            features=features,
            skills_index=skills_index() if skills_index else "",
            core_block=core_block() if core_block else "",
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
    memory: MemoryEngine
    skills: SkillStore
    openrouter: OpenRouterClient | None = None
    mcp: McpConnections | None = None
    clients: list[httpx.AsyncClient | OpenRouterClient] = field(default_factory=list)

    async def close(self) -> None:
        if self.mcp is not None:
            await self.mcp.aclose()
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
    embed_client = OpenRouterClient(settings.api_key, base_url=settings.base_url, recorder=db_recorder(db, clock)) if settings.api_key else None
    embedder = OpenRouterEmbedder(embed_client, settings.embedding_model, settings.embedding_dimensions) if embed_client else None
    memory = MemoryEngine(db, clock, settings.tz, embedder=embedder)
    skills = SkillStore(settings.home / "skills", db, clock)
    http = httpx.AsyncClient(timeout=60)
    search, extract = web_providers(settings, http)
    tools = [
        *terminal_tools(shell, processes, guards, is_fetch=detector.fetches_network),
        *file_tools(lambda: shell.cwd, ProtectedPaths(data_dirs(settings))),
        *web_tools(search, extract, secrets=settings.secrets.values),
        *memory_tools(memory),
        *alarm_tools(alarms),
        *skill_tools(skills),
    ]

    def taint(ctx: ToolContext, outcome: ToolOutcome) -> None:
        if outcome.external_source:
            gate.mark_tainted(ctx.session_id, ctx.turn_id, outcome.external_source)

    registry = ToolRegistry(tools, output_filters=output_filters(Redactor(settings.secrets.values())), on_outcome=taint)
    for tool in delegate_tools(subagent_runner(settings, db, clock, registry, gate)):
        registry.register(tool)
    clients = [http] + ([extract.http] if isinstance(extract, NativeExtractor) else []) + ([embed_client] if embed_client else [])
    return CoreTools(registry=registry, shell=shell, processes=processes, egress=gate, alarms=alarms, memory=memory, skills=skills, openrouter=embed_client, clients=clients)


AnyAgent = Agent | ClaudeCodeAgent
SHORT_QUERY = 12


def recall_hook(memory: MemoryEngine):
    async def recall(conv: Conversation, turn_id: str, items: list[InboundItem]) -> str | None:
        text = "\n".join(item.content for item in items if item.speaker != "system").strip()
        if not text:
            return None
        if len(text) < SHORT_QUERY:
            # "那個呢？" means nothing alone; lean on what the owner said just before.
            earlier = conv.messages[: len(conv.messages) - len(items)]
            previous = next((m.content for m in reversed(earlier) if m.role == "user" and m.speaker == "owner" and m.content), None)
            if previous:
                text = f"{previous}\n{text}"
        result = await memory.recall(text, session_id=conv.session_id, turn_id=turn_id, exclude=core_ids(conv.system_prompt))
        return result.block

    return recall


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
            recall=recall_hook(tools.memory),
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
        recall=recall_hook(tools.memory),
    )


SUBAGENT_TOOLS = frozenset({"terminal", "process", "read_file", "write_file", "patch", "search_files", "web_search", "web_extract", "skills_list", "skill_view"})
SUBAGENT_ITERATIONS = 25


def subagent_runner(settings: Settings, db: Database, clock: Clock, registry: ToolRegistry, gate: EgressGate, make_agent: Callable[..., "AnyAgent"] | None = None):
    """Each delegate_task call gets a fresh, memory-less agent on the current model, one level deep."""

    def default_agent(**common) -> AnyAgent:
        if current_choice(db, settings)[0].provider == CLAUDE:
            return ClaudeCodeAgent(config_dir=settings.claude_config_dir, cwd=settings.home, secrets=settings.secrets, recorder=db_recorder(db, clock), **common)
        return Agent(llm=OpenRouterClient(settings.api_key, base_url=settings.base_url, recorder=db_recorder(db, clock)), **common)

    make_agent = make_agent or default_agent

    async def run(task: str, context: str, ctx: ToolContext) -> tuple[str, str | None]:
        external: list[str] = []

        def record(tool_ctx: ToolContext, outcome: ToolOutcome) -> None:
            if outcome.external_source:
                gate.mark_tainted(tool_ctx.session_id, tool_ctx.turn_id, outcome.external_source)
                external.append(outcome.external_source)

        def started(name: str, args: dict[str, Any]) -> None:
            if registry.on_start:
                registry.on_start(name, args, sub=True)

        names = [n for n in registry.names() if n in SUBAGENT_TOOLS or n.startswith("mcp__")]
        tools = ToolRegistry((registry.get(n) for n in names), output_filters=registry.output_filters, on_outcome=record, on_start=started)
        prompt = build_subagent_prompt(timezone=settings.timezone, run_as=getpass.getuser())
        conv = Conversation.start_new(db, now=clock.now(), tz=settings.tz, system_prompt=prompt, kind="sub", parent_id=ctx.session_id)
        choice = current_choice(db, settings)[0]
        common = dict(model=choice.model, tools=tools, conversation=conv, clock=clock, max_iterations=SUBAGENT_ITERATIONS,
                      max_context_tokens=settings.max_context_tokens, effort=current_effort(db, settings)[0] or None, tool_scope=(ctx.session_id, ctx.turn_id))
        agent = make_agent(**common)
        brief = f"任務：{task}" + (f"\n\n背景：{context}" if context else "")
        try:
            result = await agent.run_turn([InboundItem("system", brief, clock.now())])
            report = result.text
        except LLMError as exc:
            report = f"子 agent 失敗了：{exc}"
        finally:
            await agent.aclose()
            conv.end(now=clock.now(), reason="done")
        return report, external[0] if external else None

    return run


async def connect_mcp(settings: Settings, tools: CoreTools, *, targets=None) -> McpConnections:
    """Connect the configured MCP servers and add their tools; must run before any agent is built."""
    connections = McpConnections()
    for tool in await connections.connect(settings.mcp_servers, secrets=settings.secrets, targets=targets):
        tools.registry.register(tool)
    tools.mcp = connections
    return connections


def needs_new_session(conversation: Conversation, choice: ModelChoice) -> bool:
    """History made on one engine cannot be handed to another, so switching provider starts a new session."""
    return conversation.engine is not None and conversation.engine != choice.provider and bool(conversation.messages)


def sleep_completer(settings: Settings, db: Database, clock: Clock, tools: CoreTools) -> Callable[[], Completer]:
    """Sleep uses [sleep] model if set, otherwise whatever niya is chatting with right now."""

    def build() -> Completer:
        choice = parse_choice(settings.sleep_model) if settings.sleep_model else current_choice(db, settings)[0]
        if choice.provider == CLAUDE:
            return ClaudeCodeCompleter(
                choice.model, config_dir=settings.claude_config_dir, cwd=settings.home, secrets=settings.secrets, recorder=db_recorder(db, clock)
            )
        if tools.openrouter is None:
            raise ValueError("睡眠整理要用 OpenRouter，但沒有設定 OPENROUTER_API_KEY。")
        return OpenRouterCompleter(tools.openrouter, choice.model)

    return build
