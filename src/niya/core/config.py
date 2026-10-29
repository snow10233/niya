import os
import re
import tomllib
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SECRET_ENV = (
    "OPENROUTER_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "DISCORD_TOKEN",
    "TAVILY_API_KEY",
    "BRAVE_SEARCH_API_KEY",
)
SYSTEM_CONFIG = Path("/etc/niya/niya.toml")
PROVIDERS = ("claude", "openrouter")
# Claude Code takes low–max; OpenRouter also takes none and minimal.
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class McpServer:
    """One MCP server: a local command over stdio, or a URL (streamable HTTP)."""

    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()  # values may reference ${VAR}; those vars are treated as secrets
    url: str = ""
    timeout_s: float = 60.0


def _mcp_servers(data: dict) -> tuple[McpServer, ...]:
    servers = []
    for name, spec in (data.get("servers") or {}).items():
        if not isinstance(spec, dict) or spec.get("enabled") is False:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise ValueError(f"MCP server 名稱只能用英數字、- 和 _：{name}")
        if bool(spec.get("command")) == bool(spec.get("url")):
            raise ValueError(f"MCP server {name} 要設定 command 或 url（擇一）")
        servers.append(
            McpServer(
                name=name,
                command=str(spec.get("command") or ""),
                args=tuple(str(a) for a in spec.get("args") or ()),
                env=tuple((str(k), str(v)) for k, v in (spec.get("env") or {}).items()),
                url=str(spec.get("url") or ""),
                timeout_s=float(spec.get("timeout_s", 60)),
            )
        )
    return tuple(servers)


@dataclass(frozen=True)
class Settings:
    home: Path
    timezone: str = "Asia/Taipei"
    provider: str = ""
    model: str = ""
    effort: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    max_iterations: int = 40
    max_context_tokens: int = 200_000
    tool_progress: bool = True
    persona_path: Path | None = None
    searxng_url: str = ""
    allow_private_urls: bool = False
    egress_allow_hosts: tuple[str, ...] = ()
    egress_taint_scope: str = "session"
    embedding_model: str = "perplexity/pplx-embed-v1-4b"
    embedding_dimensions: int = 1024
    sleep_model: str = ""
    sleep_budget_usd: float = 1.0
    mcp_servers: tuple[McpServer, ...] = ()
    discord_owner_id: int = 0
    discord_channel_ids: frozenset[int] = frozenset()
    discord_guild_id: int = 0
    secrets: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> Path:
        return self.home / "niya.db"

    @property
    def api_key(self) -> str:
        return self.secrets.get("OPENROUTER_API_KEY", "")

    @property
    def claude_config_dir(self) -> Path:
        """Claude Code keeps its login and transcripts here, apart from the owner's own ~/.claude."""
        return self.home / "claude"


def default_home(environ: MutableMapping[str, str]) -> Path:
    if environ.get("NIYA_HOME"):
        return Path(environ["NIYA_HOME"])
    if os.geteuid() == 0:
        return Path("/var/lib/niya")
    return Path.home() / ".local" / "share" / "niya"


def load_settings(
    config_path: Path | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    pop_secrets: bool = False,
) -> Settings:
    env = os.environ if environ is None else environ
    if config_path is None and env.get("NIYA_CONFIG"):
        config_path = Path(env["NIYA_CONFIG"])
    if config_path is None and SYSTEM_CONFIG.exists():
        config_path = SYSTEM_CONFIG
    data = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path else {}
    agent = data.get("agent", {})
    paths = data.get("paths", {})
    web = data.get("web", {})
    egress = data.get("egress", {})
    memory = data.get("memory", {})
    sleep = data.get("sleep", {})
    discord = data.get("discord", {})

    home = Path(env["NIYA_HOME"]) if env.get("NIYA_HOME") else Path(paths["home"]) if paths.get("home") else default_home(env)
    # Absolute from here on: subprocesses (Claude Code, the shell) run elsewhere, and the hardline only guards absolute paths.
    home = home.expanduser().resolve()
    timezone = env.get("NIYA_TIMEZONE") or agent.get("timezone", "Asia/Taipei")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"無法辨識的時區：{timezone}") from None
    persona = paths.get("persona")
    provider = env.get("NIYA_PROVIDER") or agent.get("provider", "")
    if provider and provider not in PROVIDERS:
        raise ValueError(f"未知的 provider：{provider}（可用：{'、'.join(PROVIDERS)}）")
    effort = agent.get("effort", "")
    if effort and effort not in EFFORTS:
        raise ValueError(f"未知的 effort：{effort}（可用：{'、'.join(EFFORTS)}）")

    mcp_servers = _mcp_servers(data.get("mcp", {}))
    # Tokens handed to MCP servers get the same care as niya's own keys: out of the environment, redacted everywhere.
    referenced = {ref for server in mcp_servers for _, value in server.env for ref in _ENV_REF.findall(value)}
    secret_names = (*SECRET_ENV, *sorted(referenced - set(SECRET_ENV)))
    secrets = {key: value for key in secret_names if (value := env.get(key, "").strip())}
    if pop_secrets:
        for key in secret_names:
            env.pop(key, None)

    return Settings(
        home=home,
        timezone=timezone,
        provider=provider,
        model=env.get("NIYA_MODEL") or agent.get("model", ""),
        effort=effort,
        base_url=agent.get("base_url", "https://openrouter.ai/api/v1"),
        max_iterations=int(agent.get("max_iterations", 40)),
        max_context_tokens=int(agent.get("max_context_tokens", 200_000)),
        tool_progress=bool(agent.get("tool_progress", True)),
        persona_path=Path(persona) if persona else None,
        searxng_url=env.get("SEARXNG_URL") or web.get("searxng_url", ""),
        allow_private_urls=bool(web.get("allow_private_urls", False)),
        egress_allow_hosts=tuple(egress.get("allow_hosts", ())),
        egress_taint_scope=egress.get("taint_scope", "session"),
        embedding_model=memory.get("embedding_model", "perplexity/pplx-embed-v1-4b"),
        embedding_dimensions=int(memory.get("embedding_dimensions", 1024)),
        sleep_model=env.get("NIYA_SLEEP_MODEL") or sleep.get("model", ""),
        sleep_budget_usd=float(sleep.get("budget_usd", 1.0)),
        mcp_servers=mcp_servers,
        discord_owner_id=int(env.get("NIYA_DISCORD_OWNER_ID") or discord.get("owner_id") or 0),
        discord_channel_ids=frozenset(int(c) for c in discord.get("channel_ids", ())),
        discord_guild_id=int(discord.get("guild_id") or 0),
        secrets=secrets,
    )


def resolve_env(value: str, secrets: dict[str, str]) -> str:
    """Fill ${VAR} references in an MCP server's env from the secrets niya read at startup."""
    return _ENV_REF.sub(lambda m: secrets.get(m.group(1), ""), value)
