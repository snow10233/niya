"""read_file, write_file, patch and search_files.

Schemas follow NousResearch/hermes-agent (MIT) tools/file_tools.py@222465d84;
patch uses Hermes' fuzzy matcher (tools/fuzzy_match.py, copied as-is).
"""

import asyncio
import difflib
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from niya.tools.fuzzy_match import format_no_match_hint, fuzzy_find_and_replace
from niya.tools.protected_paths import ProtectedPaths, is_sshd_config, validate_sshd
from niya.tools.registry import Tool, ToolContext, ToolError

MAX_READ_CHARS = 100_000
MAX_READ_LINES = 2000
MAX_DIFF_CHARS = 8_000

READ_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "檔案路徑（絕對路徑、相對於目前目錄，或 ~/）"},
        "offset": {"type": "integer", "description": "從第幾行開始（從 1 開始，預設 1）", "minimum": 1},
        "limit": {"type": "integer", "description": f"最多讀幾行（預設 500，最多 {MAX_READ_LINES}）", "minimum": 1},
    },
    "required": ["path"],
}
WRITE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "要寫入的檔案路徑，不存在會自動建立"},
        "content": {"type": "string", "description": "檔案的完整內容"},
    },
    "required": ["path", "content"],
}
PATCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "要修改的檔案路徑"},
        "old_string": {"type": "string", "description": "要被取代的文字；除非 replace_all=true，否則在檔案中必須唯一。多附幾行上下文來確保唯一"},
        "new_string": {"type": "string", "description": "取代後的文字；空字串代表刪除"},
        "replace_all": {"type": "boolean", "description": "取代所有出現的地方（預設 false）", "default": False},
    },
    "required": ["path", "old_string", "new_string"],
}
SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "target=content 時是正規表示式；target=files 時是檔名 glob，例如 *.py"},
        "target": {"type": "string", "enum": ["content", "files"], "default": "content"},
        "path": {"type": "string", "description": "搜尋的目錄或檔案（預設目前目錄）", "default": "."},
        "file_glob": {"type": "string", "description": "內容搜尋時只看符合這個 glob 的檔案"},
        "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "default": "content"},
        "context": {"type": "integer", "description": "每個結果前後多顯示幾行", "minimum": 0},
        "limit": {"type": "integer", "description": "最多幾筆結果（預設 50）", "minimum": 1},
        "offset": {"type": "integer", "description": "跳過前幾筆，用來翻頁", "minimum": 0},
    },
    "required": ["pattern"],
}


def _resolve(raw: Any, cwd: str) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise ToolError("path 不能是空的。")
    path = Path(text).expanduser()
    return path if path.is_absolute() else Path(cwd) / path


def _is_binary(path: Path) -> bool:
    with path.open("rb") as f:
        return b"\0" in f.read(8192)


def _suggest(path: Path) -> str:
    parent = path.parent if path.parent.is_dir() else None
    if parent is None:
        return ""
    names = [p.name for p in parent.iterdir()]
    close = difflib.get_close_matches(path.name, names, n=3, cutoff=0.5)
    return f"\n相似的檔名：{', '.join(close)}" if close else ""


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _diff(path: Path, before: str, after: str) -> str:
    diff = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), f"a/{path.name}", f"b/{path.name}"))
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n…（diff 過長，省略 {len(diff) - MAX_DIFF_CHARS} 字元）"
    return diff


def file_tools(cwd: Callable[[], str], protected: ProtectedPaths, *, sshd_validator: Callable[[Path], Awaitable[str | None]] = validate_sshd) -> list[Tool]:
    async def save(path: Path, content: str) -> None:
        if (reason := protected.check_write(path, content)) is not None:
            raise ToolError(reason)
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        if is_sshd_config(path) and previous is not None:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        atomic_write(path, content)
        if is_sshd_config(path) and (problem := await sshd_validator(path)) is not None:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, previous)
            raise ToolError(f"sshd -t 驗證失敗，已還原原本的檔案：\n{problem}")

    async def read_file(args: dict[str, Any], ctx: ToolContext) -> str:
        path = _resolve(args.get("path"), cwd())
        if not path.exists():
            raise ToolError(f"找不到檔案：{path}{_suggest(path)}")
        if path.is_dir():
            raise ToolError(f"{path} 是目錄，請用 search_files（target=files）列出內容。")
        if _is_binary(path):
            raise ToolError(f"{path} 看起來是二進位檔，無法用文字讀取。")
        offset = max(int(args.get("offset") or 1), 1)
        limit = min(max(int(args.get("limit") or 500), 1), MAX_READ_LINES)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return "（空檔案）"
        if offset > len(lines):
            return f"（檔案只有 {len(lines)} 行）"
        out, size, end = [], 0, offset - 1
        for number in range(offset, min(offset + limit, len(lines) + 1)):
            entry = f"{number}|{lines[number - 1]}"
            if size + len(entry) > MAX_READ_CHARS and out:
                break
            out.append(entry)
            size += len(entry) + 1
            end = number
        footer = f"\n…（第 {offset}–{end} 行，共 {len(lines)} 行；用 offset={end + 1} 繼續讀）" if end < len(lines) else ""
        return "\n".join(out) + footer

    async def write_file(args: dict[str, Any], ctx: ToolContext) -> str:
        path = _resolve(args.get("path"), cwd())
        if "content" not in args or not isinstance(args["content"], str):
            raise ToolError("write_file 需要字串型別的 content。")
        content = args["content"]
        existed = path.exists()
        await save(path, content)
        verb = "覆寫" if existed else "建立"
        return f"已{verb} {path}（{len(content.splitlines())} 行，{len(content.encode())} bytes）"

    async def patch(args: dict[str, Any], ctx: ToolContext) -> str:
        path = _resolve(args.get("path"), cwd())
        if not path.is_file():
            raise ToolError(f"找不到檔案：{path}{_suggest(path)}")
        old, new = args.get("old_string"), args.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError("patch 需要字串型別的 old_string 和 new_string。")
        before = path.read_text(encoding="utf-8")
        after, count, strategy, error = fuzzy_find_and_replace(before, old, new, replace_all=bool(args.get("replace_all")))
        if error:
            raise ToolError(error + format_no_match_hint(error, count, old, before))
        await save(path, after)
        note = "" if strategy in (None, "exact") else f"（用 {strategy} 比對）"
        return f"已修改 {path}，取代 {count} 處{note}：\n{_diff(path, before, after)}"

    async def search_files(args: dict[str, Any], ctx: ToolContext) -> str:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            raise ToolError("pattern 不能是空的。")
        root = _resolve(args.get("path") or ".", cwd())
        if not root.exists():
            raise ToolError(f"找不到路徑：{root}")
        limit = int(args.get("limit") or 50)
        offset = int(args.get("offset") or 0)
        if args.get("target") == "files":
            lines = await _find_files(pattern, root)
        else:
            lines = await _grep(pattern, root, args.get("file_glob"), args.get("output_mode") or "content", int(args.get("context") or 0))
        page = lines[offset : offset + limit]
        if not page:
            return "沒有找到符合的結果。"
        more = f"\n…（共 {len(lines)} 筆，用 offset={offset + limit} 看下一頁）" if offset + limit < len(lines) else ""
        return "\n".join(page) + more

    return [
        Tool(name="read_file", description="讀取文字檔，每行前面加上行號（格式 行號|內容）。讀大檔用 offset 和 limit 分段。代替 terminal 裡的 cat/head/tail。", parameters=READ_PARAMETERS, handler=read_file, toolset="file", parallel_safe=True, max_result_chars=MAX_READ_CHARS + 2000),
        Tool(name="write_file", description="把完整內容寫入檔案（整個覆寫），會自動建立上層目錄。局部修改請用 patch。代替 terminal 裡的 echo/heredoc。", parameters=WRITE_PARAMETERS, handler=write_file, toolset="file"),
        Tool(name="patch", description="在檔案裡找一段文字並取代，會容忍空白和縮排的小差異，回傳 unified diff。代替 terminal 裡的 sed/awk。", parameters=PATCH_PARAMETERS, handler=patch, toolset="file"),
        Tool(name="search_files", description="搜尋檔案內容（target=content，正規表示式）或依檔名找檔案（target=files，glob，依修改時間排序）。代替 terminal 裡的 grep/rg/find/ls。", parameters=SEARCH_PARAMETERS, handler=search_files, toolset="file", parallel_safe=True),
    ]


async def _run(*argv: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode not in (0, 1):
        raise ToolError(f"{argv[0]} 失敗：{err.decode('utf-8', errors='replace').strip()[:500]}")
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


async def _find_files(glob: str, root: Path) -> list[str]:
    if shutil.which("rg"):
        _, out = await _run("rg", "--files", "--hidden", "--glob", glob, "--glob", "!.git", str(root))
        paths = [Path(p) for p in out.splitlines() if p]
    else:
        paths = [p for p in root.rglob(glob) if p.is_file() and ".git" not in p.parts]
    paths.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return [str(p) for p in paths]


async def _grep(pattern: str, root: Path, file_glob: Any, mode: str, context: int) -> list[str]:
    if shutil.which("rg"):
        argv = ["rg", "--no-heading", "--color", "never", "--hidden", "--glob", "!.git"]
        if file_glob:
            argv += ["--glob", str(file_glob)]
        argv += ["-l"] if mode == "files_only" else ["-c"] if mode == "count" else ["--line-number"] + (["-C", str(context)] if context else [])
        argv += ["-e", pattern, str(root)]
    else:
        argv = ["grep", "-r", "-E", "--exclude-dir=.git"]
        if file_glob:
            argv.append(f"--include={file_glob}")
        argv += ["-l"] if mode == "files_only" else ["-c"] if mode == "count" else ["-n"] + (["-C", str(context)] if context else [])
        argv += ["-e", pattern, str(root)]
    _, out = await _run(*argv)
    lines = out.splitlines()
    if mode == "count":
        lines = [line for line in lines if not line.endswith(":0")]
    return lines
