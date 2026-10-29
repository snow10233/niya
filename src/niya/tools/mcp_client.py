"""MCP servers as niya tools, named mcp__<server>__<tool> and fixed when niya starts.

Whatever an MCP server returns is content from outside niya: it is wrapped as untrusted, taints the
session (so a later upload of local data needs the owner's OK), and goes through secret redaction like
every other tool result. Local servers run without niya's secrets in their environment.
"""

import logging
import os
import re
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client, StdioServerParameters

from niya.core.config import McpServer, resolve_env
from niya.tools.registry import Tool, ToolContext, ToolText
from niya.tools.shell import clean_env

log = logging.getLogger(__name__)

# Claude mode prefixes every niya tool with mcp__niya__ (11 characters); keep the whole name within 64.
MAX_NAME = 48


def tool_name(server: str, tool: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")
    return name[:MAX_NAME]


def _text(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        if getattr(block, "text", None) is not None:
            parts.append(block.text)
        else:
            parts.append(f"[{getattr(block, 'type', '內容')}]")
    structured = getattr(result, "structured_content", None)
    if not parts and structured is not None:
        parts.append(str(structured))
    return "\n".join(parts)


class McpConnections:
    """Keeps every MCP session open for the life of the process."""

    def __init__(self) -> None:
        self.stack = AsyncExitStack()
        self.errors: dict[str, str] = {}
        self.connected: list[str] = []

    async def connect(self, servers: tuple[McpServer, ...], *, secrets: Mapping[str, str], targets: Mapping[str, Any] | None = None) -> list[Tool]:
        """`targets` lets tests hand in in-process servers instead of commands or URLs."""
        tools: list[Tool] = []
        for server in servers:
            try:
                target = (targets or {}).get(server.name) or self._target(server, secrets)
                client = await self.stack.enter_async_context(Client(target, read_timeout_seconds=server.timeout_s))
                listed = await client.list_tools()
            except Exception as exc:  # one broken server must not keep niya from starting
                log.warning("MCP server %s unavailable: %s", server.name, exc)
                self.errors[server.name] = f"{type(exc).__name__}: {exc}"
                continue
            self.connected.append(server.name)
            tools += [self._tool(server, client, t) for t in listed.tools]
        return tools

    @staticmethod
    def _target(server: McpServer, secrets: Mapping[str, str]) -> Any:
        if server.url:
            return server.url
        env = clean_env(os.environ, secrets.keys())
        env.update({key: resolve_env(value, dict(secrets)) for key, value in server.env})
        return StdioServerParameters(command=server.command, args=list(server.args), env=env)

    @staticmethod
    def _tool(server: McpServer, client: Client, spec: Any) -> Tool:
        schema = getattr(spec, "input_schema", None) or getattr(spec, "inputSchema", None) or {"type": "object", "properties": {}}
        source = f"mcp:{server.name}"

        async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolText:
            result = await client.call_tool(spec.name, args)
            text = _text(result)
            if getattr(result, "is_error", False):
                text = f"錯誤：{text or 'MCP 工具回報失敗'}"
            return ToolText(text or "（沒有內容）", external=True, source=source)

        description = f"[MCP {server.name}] {getattr(spec, 'description', '') or spec.name}"
        return Tool(
            name=tool_name(server.name, spec.name),
            description=description[:1024],
            parameters=dict(schema),
            handler=handler,
            toolset=f"mcp:{server.name}",
            timeout_s=server.timeout_s + 5,
            max_result_chars=30_000,
            output_kind="external",
        )

    async def aclose(self) -> None:
        try:
            await self.stack.aclose()
        except Exception as exc:  # a server that died first must not break shutdown
            log.warning("closing MCP connections: %s", exc)
