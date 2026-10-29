"""A one-line summary of each tool call, shown to the owner while niya works.

The idea, the emoji and the choice of argument per tool follow NousResearch/hermes-agent (MIT)
agent/display.py@222465d84 (build_tool_preview), rewritten for niya's tools.
"""

from typing import Any

MAX_PREVIEW = 100

EMOJI = {
    "terminal": "💻",
    "process": "⚙️",
    "read_file": "📖",
    "write_file": "✍️",
    "patch": "🔧",
    "search_files": "🔎",
    "web_search": "🔍",
    "web_extract": "📄",
    "remember": "🧠",
    "deep_recall": "🧠",
    "recall_timeline": "🗓️",
    "recall_related": "🧠",
    "alarm": "⏰",
    "skills_list": "📚",
    "skill_view": "📚",
    "skill_manage": "📝",
    "delegate_task": "🔀",
}
MCP_EMOJI = "🔌"
DEFAULT_EMOJI = "⚡"


def _oneline(value: Any) -> str:
    return " ".join(str(value).split())


def _clip(text: str, limit: int = MAX_PREVIEW) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_text(args: dict[str, Any]) -> str:
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _alarm(args: dict[str, Any]) -> str:
    action = args.get("action", "")
    if action in ("cancel", "snooze"):
        return f"{action} #{args.get('id', '?')}"
    if action != "set":
        return str(action)
    when = args.get("at") or (f"{args['in_minutes']} 分鐘後" if args.get("in_minutes") else "")
    return f"set {args.get('title', '')}" + (f"（{when}）" if when else "")


def preview(name: str, args: dict[str, Any]) -> str:
    """The argument that tells what the call does, on one line."""
    get = args.get
    match name:
        case "terminal":
            text = _oneline(get("command", "")) + ("（背景）" if get("background") else "")
        case "process":
            text = " ".join(str(part) for part in (get("action"), get("id")) if part)
        case "read_file":
            text = str(get("path", "")) + (f"（從第 {get('offset')} 行）" if get("offset") else "")
        case "search_files":
            text = str(get("pattern", "")) + (f"（在 {get('path')}）" if get("path") else "")
        case "web_extract":
            urls = get("urls") or []
            text = ", ".join(map(str, urls)) if isinstance(urls, list) else str(urls)
        case "recall_timeline":
            text = " ~ ".join(str(part) for part in (get("since"), get("until")) if part)
        case "alarm":
            text = _alarm(args)
        case "skills_list":
            text = ""
        case "skill_view":
            text = str(get("name", "")) + (f" → {get('file_path')}" if get("file_path") else "")
        case "skill_manage":
            text = f"{get('action', '')} {get('name', '')}"
        case "delegate_task":
            text = str(get("task", ""))
        case "write_file" | "patch":
            text = str(get("path", ""))
        case "web_search" | "deep_recall":
            text = str(get("query", ""))
        case "remember":
            text = str(get("content", ""))
        case "recall_related":
            text = str(get("ref", ""))
        case _:
            text = _first_text(args)
    return _clip(_oneline(text))


def tool_line(name: str, args: dict[str, Any]) -> str:
    emoji = EMOJI.get(name) or (MCP_EMOJI if name.startswith("mcp__") else DEFAULT_EMOJI)
    text = preview(name, args)
    return f"{emoji} {name}: {text}" if text else f"{emoji} {name}"
