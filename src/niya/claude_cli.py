"""The official Claude Code binary that ships with claude-agent-sdk: its environment, login, and status.

Signing in always goes through Claude Code's own flow. niya never reads or stores the resulting
credentials; Claude Code keeps them under settings.claude_config_dir.
"""

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import claude_agent_sdk

from niya.config import Settings

AUTH_SECRETS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def bundled_cli() -> str | None:
    name = "claude"
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    if path.is_file():
        return str(path)
    return shutil.which(name)


def cli_version() -> str:
    try:
        from claude_agent_sdk._cli_version import __cli_version__
    except ImportError:
        return ""
    return __cli_version__


def claude_env(config_dir: Path, secrets: Mapping[str, str]) -> dict[str, str]:
    env = {
        # Claude Code runs in another cwd, so a relative path would point at an empty config without the login.
        "CLAUDE_CONFIG_DIR": str(config_dir.resolve()),
        # niya never compresses its context; it goes to sleep when the context fills up instead.
        "DISABLE_AUTO_COMPACT": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        # read_file returns up to ~100k characters; keep it inline instead of truncated.
        "MAX_MCP_OUTPUT_TOKENS": "120000",
        "CLAUDE_AGENT_SDK_CLIENT_APP": "niya",
    }
    env.update({key: value for key in AUTH_SECRETS if (value := secrets.get(key))})
    return env


def _run_env(settings: Settings) -> dict[str, str]:
    settings.claude_config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {**os.environ, **claude_env(settings.claude_config_dir, settings.secrets)}


async def auth_status(settings: Settings) -> dict[str, Any] | None:
    cli = bundled_cli()
    if cli is None:
        return None
    proc = await asyncio.create_subprocess_exec(
        cli, "auth", "status", "--json", env=_run_env(settings), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        proc.kill()
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def describe_auth(status: dict[str, Any] | None, settings: Settings) -> tuple[bool, str]:
    if settings.secrets.get("ANTHROPIC_API_KEY"):
        return True, "ANTHROPIC_API_KEY（API 計費，不用訂閱額度）"
    if status is None:
        return False, "無法查詢 Claude Code 的登入狀態"
    if not status.get("loggedIn"):
        return False, "尚未登入，請執行 niya login"
    method = status.get("authMethod") or "?"
    plan = status.get("subscriptionType")
    return True, f"{method}{f'（{plan}）' if plan else ''}"


def run_cli(settings: Settings, *args: str) -> int:
    """Run Claude Code interactively (login flows need the terminal)."""
    cli = bundled_cli()
    if cli is None:
        print("找不到 Claude Code 執行檔（claude-agent-sdk 沒有裝好？）")
        return 1
    return subprocess.run([cli, *args], env=_run_env(settings)).returncode
