import os
import tomllib
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SECRET_ENV = (
    "OPENROUTER_API_KEY",
    "DISCORD_TOKEN",
    "TAVILY_API_KEY",
    "BRAVE_SEARCH_API_KEY",
)
SYSTEM_CONFIG = Path("/etc/niya/niya.toml")


@dataclass(frozen=True)
class Settings:
    home: Path
    timezone: str = "Asia/Taipei"
    model: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    max_iterations: int = 40
    max_context_tokens: int = 200_000
    persona_path: Path | None = None
    searxng_url: str = ""
    allow_private_urls: bool = False
    egress_allow_hosts: tuple[str, ...] = ()
    egress_taint_scope: str = "session"
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

    home = Path(env["NIYA_HOME"]) if env.get("NIYA_HOME") else Path(paths["home"]) if paths.get("home") else default_home(env)
    timezone = env.get("NIYA_TIMEZONE") or agent.get("timezone", "Asia/Taipei")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"無法辨識的時區：{timezone}") from None
    persona = paths.get("persona")

    secrets = {key: value for key in SECRET_ENV if (value := env.get(key, "").strip())}
    if pop_secrets:
        for key in SECRET_ENV:
            env.pop(key, None)

    return Settings(
        home=home,
        timezone=timezone,
        model=env.get("NIYA_MODEL") or agent.get("model", ""),
        base_url=agent.get("base_url", "https://openrouter.ai/api/v1"),
        max_iterations=int(agent.get("max_iterations", 40)),
        max_context_tokens=int(agent.get("max_context_tokens", 200_000)),
        persona_path=Path(persona) if persona else None,
        searxng_url=env.get("SEARXNG_URL") or web.get("searxng_url", ""),
        allow_private_urls=bool(web.get("allow_private_urls", False)),
        egress_allow_hosts=tuple(egress.get("allow_hosts", ())),
        egress_taint_scope=egress.get("taint_scope", "session"),
        secrets=secrets,
    )
