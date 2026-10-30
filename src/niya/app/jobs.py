"""`niya run-job`: run a scheduled script the way Hermes' no-agent cron did, so those jobs keep working on systemd timers.

Semantics follow NousResearch/hermes-agent (MIT) cron/scheduler.py@222465d84 (the no_agent short-circuit):
stdout, trimmed and with secrets masked, goes to Discord as it is; empty stdout, or a last line of
{"wakeAgent": false}, means there is nothing to report. A non-zero exit or a timeout is reported too: a
watchdog that broke silently is the worst case.
"""

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from niya.discord_bot.text import chunk_message
from niya.tools.discord_tool import DiscordRest
from niya.tools.registry import ToolError
from niya.tools.shell import clean_env, kill_group

DEFAULT_TIMEOUT_S = 3600.0  # Hermes' own default for cron scripts
MAX_REPORT_CHARS = 16_000
FAILURE_DETAIL_CHARS = 3000
_TARGET = re.compile(r"^discord:(\d+)$")


@dataclass(frozen=True)
class JobResult:
    ok: bool
    output: str  # stdout when it worked, what went wrong when it did not


def parse_target(value: str) -> str | None:
    """A Discord channel id, or None for nowhere."""
    if value in ("", "none"):
        return None
    if match := _TARGET.match(value):
        return match.group(1)
    raise ValueError(f"看不懂的投遞目標：{value}（用 discord:<頻道 ID> 或 none）")


async def run_script(command: Sequence[str], *, cwd: Path | None, env: Mapping[str, str], timeout_s: float) -> JobResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd, env=dict(env), start_new_session=True
        )
    except OSError as exc:
        return JobResult(False, f"沒辦法啟動：{exc}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        await kill_group(proc)
        return JobResult(False, f"超過 {timeout_s:g} 秒還沒跑完，已經停掉。")
    stdout = out.decode("utf-8", errors="replace").strip()
    stderr = err.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        parts = [f"exit {proc.returncode}"]
        parts += [f"stderr：\n{stderr[-FAILURE_DETAIL_CHARS:]}"] if stderr else []
        parts += [f"stdout：\n{stdout[-FAILURE_DETAIL_CHARS:]}"] if stdout else []
        return JobResult(False, "\n".join(parts))
    return JobResult(True, stdout)


def report_of(output: str) -> str | None:
    """What a successful run has to say, if anything."""
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        gate = json.loads(lines[-1])
    except ValueError:
        gate = None
    if isinstance(gate, dict) and gate.get("wakeAgent") is False:
        return None
    if len(output) > MAX_REPORT_CHARS:
        return output[:MAX_REPORT_CHARS] + f"\n…（後面還有 {len(output) - MAX_REPORT_CHARS:,} 字沒有貼）"
    return output


def failure_report(name: str, output: str, now: float, tz: ZoneInfo) -> str:
    return f"⚠️ 排程「{name}」的腳本失敗了\n\n{output}\n\n時間：{datetime.fromtimestamp(now, tz):%Y-%m-%d %H:%M:%S}"


async def post(rest: DiscordRest, channel_id: str, text: str) -> None:
    for chunk in chunk_message(text):
        await rest.call("POST", f"/channels/{channel_id}/messages", json={"content": chunk, "allowed_mentions": {"parse": []}})


async def run_job(
    *,
    name: str,
    command: Sequence[str],
    deliver: str | None,
    on_failure: str | None,
    cwd: Path | None,
    env: Mapping[str, str],
    timeout_s: float,
    token: str,
    redact: Callable[[str], str],
    tz: ZoneInfo,
    now: Callable[[], float],
    http: httpx.AsyncClient | None = None,
) -> int:
    """0: ran (and said what it had to say); 1: the script failed; 2: it could not be delivered."""
    result = await run_script(command, cwd=cwd, env=env, timeout_s=timeout_s)
    text = report_of(redact(result.output)) if result.ok else failure_report(name, redact(result.output), now(), tz)
    target = deliver if result.ok else on_failure
    if text and target:
        if not token:
            print(f"{name}：沒有 DISCORD_TOKEN，發不出去：\n{text}")
            return 2
        client = http or httpx.AsyncClient(timeout=30)
        try:
            await post(DiscordRest(token, client), target, text)
        except (ToolError, httpx.HTTPError) as exc:
            print(f"{name}：發到 Discord 失敗（{exc}）：\n{text}")
            return 2
        finally:
            if http is None:
                await client.aclose()
    elif text:
        print(text)  # nowhere to send it: the journal keeps it
    return 0 if result.ok else 1


def child_env(base: Mapping[str, str], secret_names: Sequence[str]) -> dict[str, str]:
    """The job sees the timer's environment minus niya's own secrets, like Hermes' scripts did minus Hermes'."""
    env = clean_env(base, secret_names)
    env.pop("NIYA_AGENT", None)  # a timer job is not something niya is running
    return env


def default_cwd(command: Sequence[str]) -> Path | None:
    """Hermes ran a script from its own folder unless the job set a workdir."""
    script = next((Path(arg) for arg in reversed(command) if os.path.isabs(arg) and Path(arg).is_file()), None)
    return script.parent if script else None
