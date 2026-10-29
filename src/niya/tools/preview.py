"""What the owner sees for each tool call while niya works: a header such as "💻 terminal" and a short detail.

Ported from NousResearch/hermes-agent (MIT) @222465d84: agent/display.py build_tool_preview (which argument
tells what a call does), gateway/display_config.py (Discord caps previews at 40 characters) and gateway/run.py
(a terminal command is shown as its first line, capped the same way). Hermes puts only terminal commands in
code blocks; niya's Discord bubble does it for every tool that has a detail.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

PREVIEW_LEN = 40

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
    "recall_timeline": "🧠",
    "recall_related": "🧠",
    "alarm": "⏰",
    "skills_list": "📚",
    "skill_view": "📚",
    "skill_manage": "📝",
    "delegate_task": "🔀",
}
DEFAULT_EMOJI = "⚙️"

PRIMARY_ARGS = {
    "web_search": "query",
    "web_extract": "urls",
    "write_file": "path",
    "patch": "path",
    "search_files": "pattern",
    "skills_list": "category",
    "skill_manage": "name",
    "delegate_task": "task",
    "remember": "content",
    "deep_recall": "query",
    "recall_timeline": "since",
    "recall_related": "ref",
}
FALLBACK_ARGS = ("query", "text", "command", "path", "name", "prompt", "code", "goal")


@dataclass(frozen=True)
class ToolView:
    header: str
    detail: str = ""

    def line(self) -> str:
        return f"{self.header}: {self.detail}" if self.detail else f"{self.header}..."


def _oneline(text: str) -> str:
    return " ".join(text.split())


def _truncate(text: str, max_len: int) -> str:
    return text[: max_len - 3] + "..." if max_len > 0 and len(text) > max_len else text


def _first_line(command: str, max_len: int) -> str:
    lines = command.rstrip().splitlines()
    short = lines[0] if lines else ""
    if len(short) > max_len:
        return short[: max_len - 3] + "..."
    return short + " ..." if len(lines) > 1 else short


def _line_label(args: dict[str, Any]) -> str:
    offset, limit = args.get("offset"), args.get("limit")
    if not isinstance(offset, int) or offset <= 0:
        return ""
    if not isinstance(limit, int) or limit <= 1:
        return f"L{offset}"
    return f"L{offset}-{offset + limit - 1}"


def _alarm(args: dict[str, Any]) -> str:
    action = str(args.get("action") or "")
    if action in ("cancel", "snooze"):
        return f"{action} #{args.get('id', '?')}"
    if action != "set":
        return action
    when = args.get("at") or (f"{args['in_minutes']} 分鐘後" if args.get("in_minutes") else "")
    return f"set {args.get('title', '')}" + (f"（{when}）" if when else "")


def preview(name: str, args: dict[str, Any], max_len: int = PREVIEW_LEN) -> str:
    """The argument that tells what the call does, on one line ('' when there is none)."""
    if not args:
        return ""
    match name:
        case "terminal":
            command = args.get("command")
            return _first_line(command, max_len) if isinstance(command, str) and command.strip() else ""
        case "process":
            parts = [str(args.get("action") or "")]
            if pid := args.get("id"):
                parts.append(str(pid)[:16])
            if text := args.get("text"):
                parts.append(f'"{_oneline(str(text)[:20])}"')
            if args.get("timeout") and args.get("action") == "wait":
                parts.append(f"{args['timeout']}s")
            return " ".join(part for part in parts if part)
        case "read_file":
            path = args.get("path")
            if path is None:
                return ""
            label = PurePosixPath(str(path)).name or str(path)
            return _truncate(f"{label} {_line_label(args)}".strip(), max_len)
        case "skill_view":
            skill = _oneline(str(args.get("name") or ""))
            file_path = args.get("file_path")
            text = (f"{skill} → {_oneline(str(file_path))}" if skill else _oneline(str(file_path))) if file_path else skill
            return _truncate(text, max_len)
        case "alarm":
            return _truncate(_oneline(_alarm(args)), max_len)
    key = PRIMARY_ARGS.get(name) or next((k for k in FALLBACK_ARGS if k in args), None)
    if key is None or key not in args:
        return ""
    value = args[key]
    if isinstance(value, list):
        value = value[0] if value else ""
    return _truncate(_oneline(str(value)), max_len)


def tool_view(name: str, args: dict[str, Any], *, sub: bool = False) -> ToolView:
    header = f"{EMOJI.get(name, DEFAULT_EMOJI)} {name}"
    return ToolView(header=f"↳ {header}" if sub else header, detail=preview(name, args))
