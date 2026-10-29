"""browser_exec: drive a real browser through the Browser Use CLI.

Ported from NousResearch/hermes-agent (MIT) tools/browser_use_cli.py@222465d84: the code goes to `browser-use`
on stdin and runs as Python with the CLI's browser helpers already imported; a screenshot the code prints
comes back as an image the model looks at itself. Only the local browser daemon, no cloud backends.
"""

import asyncio
import os
import re
import shlex
import shutil
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from niya.core.media import MediaStore, image_type
from niya.tools.registry import Tool, ToolContext, ToolError, ToolText
from niya.tools.shell import clean_env, kill_group
from niya.tools.url_safety import async_is_safe_url, sensitive_query_param_name

DEFAULT_TIMEOUT_S = 300
MIN_TIMEOUT_S = 5
MAX_TIMEOUT_S = 1800
STDERR_CHARS = 4000
# Screenshot paths printed by capture_screenshot().
_IMAGE_PATH = re.compile(r"(/[^\s\"']+?\.(?:png|jpe?g|webp))", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s'\"\\)]+", re.IGNORECASE)

DESCRIPTION = (
    "用 Browser Use CLI 操作真的瀏覽器。code 會原封不動交給 browser-use，當成完整的 Python 執行（標準函式庫可用，瀏覽器輔助函式已經 import 好），"
    "stdout 會回給你。code 的第一行寫一句註解，用白話說明這一步在做什麼（例如 `# 在蝦皮搜尋鍵盤`），主人會看到它。\n\n"
    "狀態：瀏覽器分頁和 workspace 會留到下一次呼叫，Python 變數不會（每次都是新的直譯器）。workspace 的路徑在 $BH_AGENT_WORKSPACE，結果裡也會附上。"
    "要收集很多筆資料時，邊做邊寫進 workspace 的 JSON 或 CSV，最後再讀回來整理；可以重用的函式寫在 workspace 的 agent_helpers.py，每次都會自動 import。"
    "去重、計數、排序在程式裡做，不要用腦算；交出結果前核對筆數夠不夠。\n\n"
    "一個小流程（開頁、等待、擷取、操作）合成一次呼叫，不要一個動作呼叫一次；但很長的擷取分成幾次、每次寫進 workspace，逾時也不會白做。\n\n"
    "截圖：呼叫 capture_screenshot()，它會印出路徑，圖片會直接附在結果裡給你看，不用另外讀檔。\n\n"
    "輔助函式：new_tab(url) 開新分頁（第一次開網頁用這個）、goto_url(url) 在目前分頁換頁、wait_for_load() 等頁面載完、page_info() 看目前頁面的狀態、"
    "js(expr) 執行 JavaScript 並回傳結果（函式要包成 js('(() => {...})()')）、fill_input(selector, text) 填欄位、click_at_xy(x, y) 點座標、"
    "capture_screenshot() 截圖、cdp('Domain.method', **kwargs) 直接下 CDP 指令（例如 cdp('Accessibility.getFullAXTree')['nodes'] 列出所有元素，"
    "先在 Python 裡篩過再印；再用 cdp('DOM.getBoxModel', backendNodeId=n) 拿座標）、ensure_real_tab() 從壞掉或內部分頁恢復。\n\n"
    "遇到登入牆就停下來問主人，不要猜帳號密碼。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "要執行的 Python，用預先 import 好的輔助函式；需要的資料用 print(...) 印出來"},
        "timeout_s": {"type": "integer", "description": f"最多等幾秒（預設 {DEFAULT_TIMEOUT_S}，最多 {MAX_TIMEOUT_S}）", "minimum": MIN_TIMEOUT_S, "maximum": MAX_TIMEOUT_S},
    },
    "required": ["code"],
}


def find_cli(configured: str = "", path: str | None = None) -> list[str] | None:
    """[browser] command if set (e.g. "uvx browser-use"), else an installed `browser-use` (uv tool puts it in ~/.local/bin)."""
    if configured:
        return shlex.split(configured)
    search = os.pathsep.join(p for p in ((path if path is not None else os.environ.get("PATH", "")), str(Path.home() / ".local" / "bin")) if p)
    direct = shutil.which("browser-use", path=search)
    return [direct] if direct else None


async def _blocked_url(code: str, secrets: Iterable[str]) -> str | None:
    for url in dict.fromkeys(_URL.findall(code)):
        if any(secret in url for secret in secrets):
            return "網址裡含有 niya 的密鑰，不能開。"
        if (name := sensitive_query_param_name(url)) is not None:
            return f"網址裡帶有像是密鑰的參數（{name}），不能開。"
        if not await async_is_safe_url(url):
            return f"這個網址不能開（內網、本機或被擋的位址）：{url}"
    return None


def _screenshot(stdout: str, since: float) -> Path | None:
    """The last screenshot printed during this run (written after it started)."""
    for raw in reversed(_IMAGE_PATH.findall(stdout)):
        path = Path(raw)
        try:
            if path.is_file() and path.stat().st_mtime >= since - 1 and image_type(path):
                return path
        except OSError:
            continue
    return None


def browser_tools(
    command: list[str],
    *,
    workspace: Path,
    media: MediaStore,
    env: Mapping[str, str] | None = None,
    secret_names: Iterable[str] = (),
    secrets: Iterable[str] = (),
) -> list[Tool]:
    secret_values = tuple(s for s in secrets if s)

    async def browser_exec(args: dict[str, Any], ctx: ToolContext) -> ToolText:
        code = str(args.get("code") or "")
        if not code.strip():
            raise ToolError('沒有 code。例如：new_tab("https://example.com") 然後 print(page_info())。')
        if blocked := await _blocked_url(code, secret_values):
            raise ToolError(blocked)
        try:
            timeout = max(MIN_TIMEOUT_S, min(int(args.get("timeout_s") or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        workspace.mkdir(parents=True, exist_ok=True)
        run_env = clean_env(env if env is not None else os.environ, secret_names)
        run_env["BH_AGENT_WORKSPACE"] = str(workspace)
        started = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=run_env, cwd=str(workspace), start_new_session=True,
            )
        except OSError as exc:
            raise ToolError(f"browser-use 啟動失敗：{exc}") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(code.encode("utf-8")), timeout)
        except TimeoutError:
            await kill_group(proc)
            raise ToolError(
                f"browser-use 執行超過 {timeout} 秒，已經停掉。可以把 timeout_s 調大（最多 {MAX_TIMEOUT_S}），"
                "或拆成幾次呼叫、每次把進度寫進 workspace（已經寫進去的都還在）。"
            ) from None
        except asyncio.CancelledError:
            await kill_group(proc)
            raise
        stdout = out.decode("utf-8", errors="replace")
        stderr = err.decode("utf-8", errors="replace").strip()
        lines = [f"exit {proc.returncode}，workspace：{workspace}", stdout.rstrip() or "（沒有輸出）"]
        if stderr:
            lines.append("stderr：\n" + (stderr[:STDERR_CHARS] + "\n…（stderr 太長，後面省略）" if len(stderr) > STDERR_CHARS else stderr))
        images: tuple[str, ...] = ()
        if shot := _screenshot(stdout, started):
            # The CLI reuses its screenshot file; keep a copy so this result always shows this picture.
            copy = media.path_for("screenshots", shot.suffix.lower(), now=started)
            shutil.copyfile(shot, copy)
            images = (str(copy),)
            lines.append(f"截圖：{copy}（附在這裡）")
        return ToolText("\n".join(lines), external=True, source="browser", images=images)

    return [
        Tool(
            name="browser_exec", description=DESCRIPTION, parameters=PARAMETERS, handler=browser_exec, toolset="browser",
            timeout_s=MAX_TIMEOUT_S + 60, max_result_chars=30_000, output_kind="external",
        )
    ]
