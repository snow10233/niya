"""The `terminal` and `process` tools.

Schema wording and `interpret_exit_code` are adapted from
NousResearch/hermes-agent (MIT) tools/terminal_tool.py@222465d84.
"""

import re
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from niya.tools.hardline import Hardline
from niya.tools.processes import ProcessRegistry
from niya.tools.registry import Tool, ToolContext, ToolError
from niya.tools.shell import ShellSession

DEFAULT_TIMEOUT_S = 180
MAX_TIMEOUT_S = 1800

CommandGuard = Callable[[str, ToolContext], Awaitable[str | None]]

TERMINAL_DESCRIPTION = """在 VPS 上執行 shell 指令。工作目錄、export 的環境變數、函式和 alias 會在呼叫之間保留。

讀檔用 read_file、找檔案或內容用 search_files、改檔用 patch 或 write_file；terminal 留給建置、安裝套件、git、管理程序、跑 script、網路操作這類真的需要 shell 的事。
前景執行（預設）：指令一結束就回傳，所以長時間的建置可以把 timeout 設大一點。
背景執行：background=true 會回傳程序 id，用 process 工具查看。會自己結束的工作加上 notify_on_complete=true，結束時你會收到通知。不要用 nohup、setsid 或結尾的 &。要一直跑的服務請做成 systemd unit 或用 systemd-run，背景程序在 niya 重啟時會被關掉。
互動式指令會拿到空的 stdin；git 等可能分頁的指令已經設定成不分頁。"""

TERMINAL_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要執行的 shell 指令"},
        "background": {"type": "boolean", "description": "在背景執行並立刻回傳程序 id", "default": False},
        "notify_on_complete": {"type": "boolean", "description": "背景程序結束時通知你（只在 background=true 時有效）", "default": False},
        "timeout": {"type": "integer", "description": f"前景指令的逾時秒數，預設 {DEFAULT_TIMEOUT_S}，最多 {MAX_TIMEOUT_S}", "minimum": 1},
        "workdir": {"type": "string", "description": "只對這個指令生效的工作目錄（絕對路徑）"},
    },
    "required": ["command"],
}

PROCESS_DESCRIPTION = """管理 terminal 以 background=true 啟動的背景程序。
- list：列出所有背景程序
- poll：查看狀態和上次查看之後的新輸出
- log：查看最後幾行輸出（lines，預設 50）
- wait：等它結束（timeout 秒，預設 60）
- kill：結束程序
- write：寫入 stdin（text，需要換行時請自己加 \\n）"""

PROCESS_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["list", "poll", "log", "wait", "kill", "write"]},
        "id": {"type": "string", "description": "程序 id，例如 p1（list 以外都需要）"},
        "lines": {"type": "integer", "minimum": 1},
        "timeout": {"type": "integer", "minimum": 1},
        "text": {"type": "string"},
    },
    "required": ["action"],
}

_EXIT_NOTES: dict[str, dict[int, str]] = {
    **{cmd: {1: "沒有找到符合的結果（不是錯誤）"} for cmd in ("grep", "egrep", "fgrep", "rg", "ag", "ack")},
    "diff": {1: "檔案內容不同（預期內，不是錯誤）"},
    "colordiff": {1: "檔案內容不同（預期內，不是錯誤）"},
    "find": {1: "有些目錄無法存取（結果可能仍然有效）"},
    "test": {1: "條件不成立（預期內，不是錯誤）"},
    "[": {1: "條件不成立（預期內，不是錯誤）"},
    "curl": {6: "無法解析主機名稱", 7: "無法連線到主機", 22: "HTTP 回應碼表示錯誤（例如 404、500）", 28: "操作逾時"},
    "git": {1: "非零結束碼（常是正常的，例如 git diff 有差異時會回傳 1）"},
}


def interpret_exit_code(command: str, exit_code: int) -> str | None:
    if exit_code == 0:
        return None
    segments = re.split(r"\s*(?:\|\||&&|[|;])\s*", command)
    words = (segments[-1] if segments else command).strip().split()
    base = next((w.split("/")[-1] for w in words if not ("=" in w and not w.startswith("-"))), "")
    return _EXIT_NOTES.get(base, {}).get(exit_code)


def hardline_guard(hardline: Hardline) -> CommandGuard:
    async def guard(command: str, ctx: ToolContext) -> str | None:
        reason = hardline.check(command)
        if reason is None:
            return None
        return f"⛔ 這個指令被黑名單擋下，沒有執行（{reason}）。不要想辦法繞過，請直接告訴主人原因。"

    return guard


def _workdir(value: Any) -> str | None:
    if value in (None, ""):
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise ToolError("workdir 必須是絕對路徑。")
    if not path.is_dir():
        raise ToolError(f"workdir 不存在或不是目錄：{path}")
    return str(path)


def terminal_tools(shell: ShellSession, processes: ProcessRegistry, guards: Sequence[CommandGuard]) -> list[Tool]:
    async def terminal(args: dict[str, Any], ctx: ToolContext) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ToolError("command 不能是空的。")
        for guard in guards:
            if (message := await guard(command, ctx)) is not None:
                raise ToolError(message)
        workdir = _workdir(args.get("workdir"))

        if args.get("background"):
            try:
                bg = await processes.start(command, shell=shell, cwd=workdir, notify=bool(args.get("notify_on_complete")))
            except RuntimeError as exc:
                raise ToolError(str(exc)) from None
            note = "結束時會通知你。" if bg.notify else "用 process 工具查看輸出和狀態。"
            return f"已在背景啟動 {bg.id}（pid {bg.proc.pid}）：{command}\n{note}"

        timeout = min(max(int(args.get("timeout") or DEFAULT_TIMEOUT_S), 1), MAX_TIMEOUT_S)
        before = shell.cwd
        result = await shell.run(command, timeout=timeout, cwd=workdir)
        lines = [result.output.rstrip("\n") or "（沒有輸出）"]
        if result.timed_out:
            lines.append(f"[逾時：超過 {timeout} 秒，已中止。長時間的工作請用 background=true]")
        else:
            note = interpret_exit_code(command, result.exit_code)
            lines.append(f"[exit {result.exit_code}{'：' + note if note else ''}]")
        if workdir is None and shell.cwd != before:
            lines.append(f"[目前目錄：{shell.cwd}]")
        return "\n".join(lines)

    async def process(args: dict[str, Any], ctx: ToolContext) -> str:
        action = args.get("action")
        if action == "list":
            items = processes.list()
            if not items:
                return "目前沒有背景程序。"
            return "\n".join(f"{p.id}：{p.status(ctx.now())}｜{p.command}" for p in items)
        pid = str(args.get("id") or "")
        if not pid:
            raise ToolError("這個動作需要 id。")
        try:
            bg = processes.get(pid)
        except KeyError as exc:
            raise ToolError(str(exc.args[0])) from None
        if action == "poll":
            return f"{bg.id}：{bg.status(ctx.now())}\n{bg.unread() or '（沒有新輸出）'}"
        if action == "log":
            return bg.tail_lines(int(args.get("lines") or 50)) or "（沒有輸出）"
        if action == "wait":
            finished = await processes.wait(pid, float(args.get("timeout") or 60))
            head = f"{bg.id}：{bg.status(ctx.now())}" if finished else f"{bg.id} 還在執行，等待逾時。"
            return f"{head}\n{bg.unread() or '（沒有新輸出）'}"
        if action == "kill":
            await processes.kill(pid)
            return f"{bg.id}：{bg.status(ctx.now())}"
        if action == "write":
            try:
                await processes.write(pid, str(args.get("text") or ""))
            except RuntimeError as exc:
                raise ToolError(str(exc)) from None
            return f"已寫入 {bg.id} 的 stdin。"
        raise ToolError(f"未知的 action：{action}")

    return [
        Tool(name="terminal", description=TERMINAL_DESCRIPTION, parameters=TERMINAL_PARAMETERS, handler=terminal, toolset="terminal", timeout_s=MAX_TIMEOUT_S + 60, max_result_chars=30_000),
        Tool(name="process", description=PROCESS_DESCRIPTION, parameters=PROCESS_PARAMETERS, handler=process, toolset="terminal", timeout_s=3600, max_result_chars=30_000),
    ]
